"""Secrets API subject isolation tests — Router + Service layer.

Tests that every endpoint correctly extracts subject_id/subject_type from
auth_result and passes them to the service layer, and that the service layer
enforces subject-based filtering so users can only access their own secrets.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

src_path = Path(__file__).parent.parent.parent.parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Auth result fixtures
# ---------------------------------------------------------------------------

AUTH_USER_ALICE = {
    "authenticated": True,
    "subject_id": "user-alice",
    "subject_type": "user",
    "zone_id": "root",
    "is_admin": False,
}

AUTH_USER_BOB = {
    "authenticated": True,
    "subject_id": "user-bob",
    "subject_type": "user",
    "zone_id": "root",
    "is_admin": False,
}

AUTH_AGENT_CARL = {
    "authenticated": True,
    "subject_id": "agent-carl",
    "subject_type": "agent",
    "zone_id": "root",
    "is_admin": False,
}

AUTH_ANONYMOUS = {
    "authenticated": True,
    "subject_id": None,
    "subject_type": None,
    "zone_id": "root",
    "is_admin": False,
}


# ---------------------------------------------------------------------------
# Test app factory (mock service, override auth)
# ---------------------------------------------------------------------------

def _create_test_app(auth_result):
    from nexus.server.api.v2.routers.secrets import get_secrets_service, router
    from nexus.server.dependencies import require_auth

    app = FastAPI()
    app.include_router(router)

    mock_service = MagicMock()
    mock_service.put_secret.return_value = {"namespace": "ns", "key": "k", "version": 1}
    mock_service.get_secret.return_value = {"namespace": "ns", "key": "k", "value": "v", "version": 1}
    mock_service.list_secrets.return_value = []
    mock_service.list_versions.return_value = []
    mock_service.delete_secret.return_value = True
    mock_service.restore_secret.return_value = True
    mock_service.delete_version.return_value = True
    mock_service.enable_secret.return_value = True
    mock_service.disable_secret.return_value = True
    mock_service.update_description.return_value = True
    mock_service.batch_put.return_value = []
    mock_service.batch_get.return_value = {}

    app.dependency_overrides[get_secrets_service] = lambda: mock_service

    if auth_result is not None:
        app.dependency_overrides[require_auth] = lambda: auth_result
    else:
        async def _no_auth():
            raise HTTPException(status_code=401, detail="Unauthorized")
        app.dependency_overrides[require_auth] = _no_auth

    return app, mock_service


# ===========================================================================
# 5a. Router layer — verify subject_id/subject_type passed to service
# ===========================================================================


class TestRouterSubjectPassthrough:
    """Verify every endpoint extracts subject_id/subject_type and passes to service."""

    @pytest.fixture
    def client_and_mock(self):
        app, mock_svc = _create_test_app(AUTH_USER_ALICE)
        client = TestClient(app, raise_server_exceptions=False)
        return client, mock_svc

    def test_put_secret_passes_subject(self, client_and_mock):
        c, m = client_and_mock
        c.put("/api/v2/secrets/ns/k", json={"value": "v"})
        m.put_secret.assert_called_once()
        kwargs = m.put_secret.call_args.kwargs
        assert kwargs["subject_id"] == "user-alice"
        assert kwargs["subject_type"] == "user"

    def test_get_secret_passes_subject(self, client_and_mock):
        c, m = client_and_mock
        c.get("/api/v2/secrets/ns/k")
        m.get_secret.assert_called_once()
        kwargs = m.get_secret.call_args.kwargs
        assert kwargs["subject_id"] == "user-alice"
        assert kwargs["subject_type"] == "user"

    def test_list_secrets_passes_subject(self, client_and_mock):
        c, m = client_and_mock
        c.get("/api/v2/secrets")
        m.list_secrets.assert_called_once()
        kwargs = m.list_secrets.call_args.kwargs
        assert kwargs["subject_id"] == "user-alice"
        assert kwargs["subject_type"] == "user"

    def test_list_versions_passes_subject(self, client_and_mock):
        c, m = client_and_mock
        c.get("/api/v2/secrets/ns/k/versions")
        m.list_versions.assert_called_once()
        kwargs = m.list_versions.call_args.kwargs
        assert kwargs["subject_id"] == "user-alice"
        assert kwargs["subject_type"] == "user"

    def test_delete_secret_passes_subject(self, client_and_mock):
        c, m = client_and_mock
        c.delete("/api/v2/secrets/ns/k")
        m.delete_secret.assert_called_once()
        kwargs = m.delete_secret.call_args.kwargs
        assert kwargs["subject_id"] == "user-alice"
        assert kwargs["subject_type"] == "user"

    def test_restore_secret_passes_subject(self, client_and_mock):
        c, m = client_and_mock
        c.post("/api/v2/secrets/ns/k/restore")
        m.restore_secret.assert_called_once()
        kwargs = m.restore_secret.call_args.kwargs
        assert kwargs["subject_id"] == "user-alice"
        assert kwargs["subject_type"] == "user"

    def test_delete_version_passes_subject(self, client_and_mock):
        c, m = client_and_mock
        c.delete("/api/v2/secrets/ns/k/versions/1")
        m.delete_version.assert_called_once()
        kwargs = m.delete_version.call_args.kwargs
        assert kwargs["subject_id"] == "user-alice"
        assert kwargs["subject_type"] == "user"

    def test_enable_secret_passes_subject(self, client_and_mock):
        c, m = client_and_mock
        c.put("/api/v2/secrets/ns/k/enable")
        m.enable_secret.assert_called_once()
        kwargs = m.enable_secret.call_args.kwargs
        assert kwargs["subject_id"] == "user-alice"
        assert kwargs["subject_type"] == "user"

    def test_disable_secret_passes_subject(self, client_and_mock):
        c, m = client_and_mock
        c.put("/api/v2/secrets/ns/k/disable")
        m.disable_secret.assert_called_once()
        kwargs = m.disable_secret.call_args.kwargs
        assert kwargs["subject_id"] == "user-alice"
        assert kwargs["subject_type"] == "user"

    def test_update_description_passes_subject(self, client_and_mock):
        c, m = client_and_mock
        c.put("/api/v2/secrets/ns/k/description", json={"description": "d"})
        m.update_description.assert_called_once()
        kwargs = m.update_description.call_args.kwargs
        assert kwargs["subject_id"] == "user-alice"
        assert kwargs["subject_type"] == "user"

    def test_batch_put_passes_subject(self, client_and_mock):
        c, m = client_and_mock
        c.post("/api/v2/secrets/batch", json=[{"namespace": "ns", "key": "k", "value": "v"}])
        m.batch_put.assert_called_once()
        kwargs = m.batch_put.call_args.kwargs
        assert kwargs["subject_id"] == "user-alice"
        assert kwargs["subject_type"] == "user"

    def test_batch_get_passes_subject(self, client_and_mock):
        c, m = client_and_mock
        c.post("/api/v2/secrets/batch/get", json=[{"namespace": "ns", "key": "k"}])
        m.batch_get.assert_called_once()
        kwargs = m.batch_get.call_args.kwargs
        assert kwargs["subject_id"] == "user-alice"
        assert kwargs["subject_type"] == "user"

    def test_anonymous_auth_falls_back_to_defaults(self):
        """When subject_id is None, router falls back to 'anonymous'/'user'."""
        app, mock_svc = _create_test_app(AUTH_ANONYMOUS)
        client = TestClient(app, raise_server_exceptions=False)
        client.put("/api/v2/secrets/ns/k", json={"value": "v"})
        kwargs = mock_svc.put_secret.call_args.kwargs
        assert kwargs["subject_id"] == "anonymous"
        assert kwargs["subject_type"] == "user"


# ===========================================================================
# 5b. Service layer — subject isolation with real database
# ===========================================================================


class TestServiceSubjectIsolation:
    """Integration tests with in-memory SQLite to verify subject filtering."""

    @pytest.fixture
    def service(self):
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker

        # Import models so they register with Base.metadata
        import nexus.storage.models.secret_store  # noqa: F401
        from nexus.storage.models._base import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)

        factory = sessionmaker(bind=engine)

        # Enable WAL mode for SQLite (required by some features)
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))

        mock_rs = MagicMock()
        mock_rs.session_factory = factory

        mock_crypto = MagicMock()
        mock_crypto.encrypt_token.side_effect = lambda v: f"enc({v})"
        mock_crypto.decrypt_token.side_effect = lambda v: v.replace("enc(", "").replace(")", "")

        from nexus.bricks.secrets.service import SecretsService
        from nexus.storage.secrets_audit_logger import SecretsAuditLogger

        # Use real audit logger with in-memory DB
        real_audit = SecretsAuditLogger(record_store=mock_rs)

        svc = SecretsService(
            record_store=mock_rs,
            oauth_crypto=mock_crypto,
            audit_logger=real_audit,
        )
        yield svc
        engine.dispose()

    # -- S1: Same-user full CRUD lifecycle --

    def test_same_user_crud_lifecycle(self, service):
        service.put_secret("ns", "k", "val1", subject_id="alice", subject_type="user")
        result = service.get_secret("ns", "k", subject_id="alice", subject_type="user")
        assert result["value"] == "val1"

        service.delete_secret("ns", "k", subject_id="alice", subject_type="user")
        result = service.get_secret("ns", "k", subject_id="alice", subject_type="user")
        assert result is None

    # -- S2: Cross-user read isolation --

    def test_cross_user_read_isolation(self, service):
        service.put_secret("ns", "k", "val", subject_id="alice", subject_type="user")
        result = service.get_secret("ns", "k", subject_id="bob", subject_type="user")
        assert result is None

    # -- S3: Cross-user list isolation --

    def test_cross_user_list_isolation(self, service):
        service.put_secret("ns", "k", "val", subject_id="alice", subject_type="user")
        secrets = service.list_secrets(subject_id="bob", subject_type="user")
        assert secrets == []

    # -- S4: Cross-user delete isolation --

    def test_cross_user_delete_isolation(self, service):
        service.put_secret("ns", "k", "val", subject_id="alice", subject_type="user")
        result = service.delete_secret("ns", "k", subject_id="bob", subject_type="user")
        assert result is False
        # Alice's secret still accessible
        assert service.get_secret("ns", "k", subject_id="alice", subject_type="user") is not None

    # -- S5: Cross-user update description isolation --

    def test_cross_user_update_description_isolation(self, service):
        service.put_secret("ns", "k", "val", subject_id="alice", subject_type="user")
        result = service.update_description("ns", "k", "new desc", subject_id="bob", subject_type="user")
        assert result is False

    # -- S6: Cross-user enable/disable isolation --

    def test_cross_user_enable_disable_isolation(self, service):
        service.put_secret("ns", "k", "val", subject_id="alice", subject_type="user")
        assert service.disable_secret("ns", "k", subject_id="bob", subject_type="user") is False
        assert service.enable_secret("ns", "k", subject_id="bob", subject_type="user") is False
        # Alice can still disable her own
        assert service.disable_secret("ns", "k", subject_id="alice", subject_type="user") is True

    # -- S7: Cross-user restore isolation --

    def test_cross_user_restore_isolation(self, service):
        service.put_secret("ns", "k", "val", subject_id="alice", subject_type="user")
        service.delete_secret("ns", "k", subject_id="alice", subject_type="user")
        assert service.restore_secret("ns", "k", subject_id="bob", subject_type="user") is False

    # -- S8: Cross-user list_versions isolation --

    def test_cross_user_list_versions_isolation(self, service):
        service.put_secret("ns", "k", "v1", subject_id="alice", subject_type="user")
        service.put_secret("ns", "k", "v2", subject_id="alice", subject_type="user")
        versions = service.list_versions("ns", "k", subject_id="bob", subject_type="user")
        assert versions == []

    # -- S9: Cross-user delete_version isolation --

    def test_cross_user_delete_version_isolation(self, service):
        service.put_secret("ns", "k", "v1", subject_id="alice", subject_type="user")
        service.put_secret("ns", "k", "v2", subject_id="alice", subject_type="user")
        result = service.delete_version("ns", "k", 1, subject_id="bob", subject_type="user")
        assert result is False

    # -- S10: Same namespace+key, different users --

    def test_same_namespace_key_different_users(self, service):
        service.put_secret("ns", "k", "alice_val", subject_id="alice", subject_type="user")
        service.put_secret("ns", "k", "bob_val", subject_id="bob", subject_type="user")

        alice_val = service.get_secret("ns", "k", subject_id="alice", subject_type="user")
        bob_val = service.get_secret("ns", "k", subject_id="bob", subject_type="user")

        assert alice_val["value"] == "alice_val"
        assert bob_val["value"] == "bob_val"

    # -- S11: Different subject_type, same subject_id --

    def test_different_subject_type_same_subject_id(self, service):
        service.put_secret("ns", "k", "user_val", subject_id="alice", subject_type="user")
        service.put_secret("ns", "k", "agent_val", subject_id="alice", subject_type="agent")

        user_val = service.get_secret("ns", "k", subject_id="alice", subject_type="user")
        agent_val = service.get_secret("ns", "k", subject_id="alice", subject_type="agent")

        assert user_val["value"] == "user_val"
        assert agent_val["value"] == "agent_val"

    # -- S12: batch_get isolation --

    def test_batch_get_isolation(self, service):
        service.put_secret("ns", "k1", "v1", subject_id="alice", subject_type="user")
        service.put_secret("ns", "k2", "v2", subject_id="alice", subject_type="user")

        results = service.batch_get(
            [{"namespace": "ns", "key": "k1"}, {"namespace": "ns", "key": "k2"}],
            subject_id="bob",
            subject_type="user",
        )
        assert results == {}

    # -- S13: list_secrets returns subject fields --

    def test_list_secrets_returns_subject_fields(self, service):
        service.put_secret("ns", "k", "val", subject_id="alice", subject_type="user")
        secrets = service.list_secrets(subject_id="alice", subject_type="user")
        assert len(secrets) == 1
        assert secrets[0]["subject_id"] == "alice"
        assert secrets[0]["subject_type"] == "user"

    # -- S14: Same user multiple updates --

    def test_same_user_multiple_updates(self, service):
        service.put_secret("ns", "k", "v1", subject_id="alice", subject_type="user")
        service.put_secret("ns", "k", "v2", subject_id="alice", subject_type="user")
        result = service.get_secret("ns", "k", subject_id="alice", subject_type="user")
        assert result["value"] == "v2"
        assert result["version"] == 2

    # -- S15: Cross-user cannot overwrite --

    def test_cross_user_cannot_overwrite(self, service):
        service.put_secret("ns", "k", "alice_val", subject_id="alice", subject_type="user")
        service.put_secret("ns", "k", "bob_val", subject_id="bob", subject_type="user")
        # Alice still gets her own value
        result = service.get_secret("ns", "k", subject_id="alice", subject_type="user")
        assert result["value"] == "alice_val"

    # -- S16: Soft delete then restore by same user --

    def test_soft_delete_then_restore_same_user(self, service):
        service.put_secret("ns", "k", "val", subject_id="alice", subject_type="user")
        service.delete_secret("ns", "k", subject_id="alice", subject_type="user")
        assert service.get_secret("ns", "k", subject_id="alice", subject_type="user") is None
        service.restore_secret("ns", "k", subject_id="alice", subject_type="user")
        result = service.get_secret("ns", "k", subject_id="alice", subject_type="user")
        assert result["value"] == "val"

    # -- S17: Anonymous mode isolation --

    def test_anonymous_mode_isolation(self, service):
        service.put_secret("ns", "k", "anon_val", subject_id="anonymous", subject_type="user")
        result = service.get_secret("ns", "k", subject_id="alice", subject_type="user")
        assert result is None

    # -- S18: Anonymous mode self-access --

    def test_anonymous_mode_self_access(self, service):
        service.put_secret("ns", "k", "anon_val", subject_id="anonymous", subject_type="user")
        result = service.get_secret("ns", "k", subject_id="anonymous", subject_type="user")
        assert result["value"] == "anon_val"

    # -- S19: Anonymous mode list isolation --

    def test_anonymous_mode_list_isolation(self, service):
        service.put_secret("ns", "k", "anon_val", subject_id="anonymous", subject_type="user")
        alice_list = service.list_secrets(subject_id="alice", subject_type="user")
        anon_list = service.list_secrets(subject_id="anonymous", subject_type="user")
        assert alice_list == []
        assert len(anon_list) == 1
