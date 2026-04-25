"""Tests for scoped API key creation with per-path grants (Issue #3128).

Tests the grants extension to POST /api/v2/auth/keys using a real
EnhancedReBACManager backed by in-memory SQLite.
"""

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.bricks.rebac.consistency.metastore_namespace_store import (
    MetastoreNamespaceStore,
)
from nexus.bricks.rebac.manager import EnhancedReBACManager
from nexus.server.api.v2.routers.auth_keys import (
    ROLE_TO_RELATION,
    GrantRequest,
    router,
)
from tests.helpers.in_memory_record_store import InMemoryRecordStore
from tests.helpers.inmemory_nexus_fs import InMemoryNexusFS

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def record_store():
    store = InMemoryRecordStore()
    # Create rebac_namespaces table (removed from ORM models in #183 migration)
    from sqlalchemy import text

    with store.engine.connect() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS rebac_namespaces ("
                "  namespace_id TEXT PRIMARY KEY,"
                "  object_type TEXT UNIQUE NOT NULL,"
                "  config TEXT NOT NULL,"
                "  created_at TEXT NOT NULL,"
                "  updated_at TEXT NOT NULL"
                ")"
            )
        )
        conn.commit()
    yield store
    store.close()


@pytest.fixture()
def rebac_manager(record_store):
    manager = EnhancedReBACManager(
        engine=record_store.engine,
        cache_ttl_seconds=1,  # Short TTL — tests don't need caching
        max_depth=10,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    # Disable all caching — this test checks revocation correctness, not
    # cache behavior. Without this, the coordinator's background recompute
    # executor and L1 cache cause "closed database" races during teardown.
    manager._l1_cache = None
    manager._boundary_cache = None
    manager._cache_coordinator._async_recompute_enabled = False
    manager._cache_coordinator._stream = None
    yield manager
    manager.close()


@pytest.fixture()
def mock_db_provider(record_store):
    """A mock auth provider with a real session_factory for key creation."""
    provider = MagicMock(spec=["session_factory", "_record_store"])
    provider.session_factory = record_store.session_factory
    provider._record_store = record_store
    return provider


@pytest.fixture()
def client(mock_db_provider, rebac_manager):
    """TestClient with admin dependency overridden and real rebac_manager."""
    from nexus.server.dependencies import require_admin

    app = FastAPI()
    app.include_router(router)

    # Override admin dependency to always pass
    app.dependency_overrides[require_admin] = lambda: None

    # Wire up app.state
    app.state.auth_provider = mock_db_provider
    app.state.rebac_manager = rebac_manager

    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests — backward compatibility (no grants)
# ---------------------------------------------------------------------------


class TestCreateKeyWithoutGrants:
    """Existing behavior must be unchanged when grants is omitted."""

    def test_create_key_no_grants(self, client):
        resp = client.post(
            "/api/v2/auth/keys",
            json={"name": "basic-key", "is_admin": False},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "key_id" in data
        assert "key" in data
        assert "grants" not in data

    def test_create_key_grants_null(self, client):
        resp = client.post(
            "/api/v2/auth/keys",
            json={"name": "null-grants", "grants": None},
        )
        assert resp.status_code == 201
        assert "grants" not in resp.json()

    def test_create_key_grants_empty_list(self, client):
        resp = client.post(
            "/api/v2/auth/keys",
            json={"name": "empty-grants", "grants": []},
        )
        assert resp.status_code == 201
        # Empty list is falsy, so no grants in response
        assert "grants" not in resp.json()


# ---------------------------------------------------------------------------
# Tests — grants creation
# ---------------------------------------------------------------------------


class TestCreateKeyWithGrants:
    """Key creation with per-path ReBAC grants."""

    def test_single_grant(self, client, rebac_manager):
        resp = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "alice",
                "subject_type": "user",
                "grants": [{"path": "/workspace/project-a/*", "role": "editor"}],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "grants" in data
        assert len(data["grants"]) == 1
        assert data["grants"][0] == {"path": "/workspace/project-a/*", "role": "editor"}

    def test_multiple_grants(self, client, rebac_manager):
        resp = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "bob",
                "subject_type": "user",
                "grants": [
                    {"path": "/workspace/project-a/*", "role": "editor"},
                    {"path": "/workspace/shared/*", "role": "viewer"},
                    {"path": "/workspace/admin/*", "role": "owner"},
                ],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data["grants"]) == 3
        roles = {g["role"] for g in data["grants"]}
        assert roles == {"editor", "viewer", "owner"}

    def test_grants_create_rebac_tuples(self, client, rebac_manager):
        resp = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "carol",
                "user_id": "carol-123",
                "subject_type": "user",
                "grants": [{"path": "/docs/readme.md", "role": "viewer"}],
            },
        )
        assert resp.status_code == 201

        # Verify the ReBAC tuple was actually created
        has_perm = rebac_manager.rebac_check(
            subject=("user", "carol-123"),
            permission="read",
            object=("file", "/docs/readme.md"),
        )
        assert has_perm is True

        # Viewer should NOT have write permission
        has_write = rebac_manager.rebac_check(
            subject=("user", "carol-123"),
            permission="write",
            object=("file", "/docs/readme.md"),
        )
        assert has_write is False

    def test_editor_grant_gives_read_and_write(self, client, rebac_manager):
        resp = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "dave",
                "user_id": "dave-456",
                "subject_type": "user",
                "grants": [{"path": "/src/main.py", "role": "editor"}],
            },
        )
        assert resp.status_code == 201

        assert rebac_manager.rebac_check(
            subject=("user", "dave-456"),
            permission="read",
            object=("file", "/src/main.py"),
        )
        assert rebac_manager.rebac_check(
            subject=("user", "dave-456"),
            permission="write",
            object=("file", "/src/main.py"),
        )

    def test_grants_with_agent_subject_type(self, client, rebac_manager):
        resp = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "agent-key",
                "user_id": "agent-007",
                "subject_type": "agent",
                "grants": [{"path": "/workspace/tools/*", "role": "editor"}],
            },
        )
        assert resp.status_code == 201

        has_perm = rebac_manager.rebac_check(
            subject=("agent", "agent-007"),
            permission="write",
            object=("file", "/workspace/tools/*"),
        )
        assert has_perm is True

    def test_grants_with_expiry(self, client):
        resp = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "temp-key",
                "expires_days": 30,
                "grants": [{"path": "/tmp/*", "role": "viewer"}],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["expires_at"] is not None
        assert len(data["grants"]) == 1


# ---------------------------------------------------------------------------
# Tests — validation
# ---------------------------------------------------------------------------


class TestGrantValidation:
    """Input validation for grant fields."""

    def test_invalid_role(self, client):
        resp = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "bad-role",
                "grants": [{"path": "/foo", "role": "superadmin"}],
            },
        )
        assert resp.status_code == 422

    def test_relative_path(self, client):
        resp = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "rel-path",
                "grants": [{"path": "relative/path", "role": "viewer"}],
            },
        )
        assert resp.status_code == 422

    def test_path_traversal(self, client):
        resp = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "traversal",
                "grants": [{"path": "/workspace/../secrets/key", "role": "viewer"}],
            },
        )
        assert resp.status_code == 422

    def test_empty_path(self, client):
        resp = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "empty",
                "grants": [{"path": "", "role": "viewer"}],
            },
        )
        assert resp.status_code == 422

    def test_too_many_grants(self, client):
        grants = [{"path": f"/path/{i}", "role": "viewer"} for i in range(101)]
        resp = client.post(
            "/api/v2/auth/keys",
            json={"name": "too-many", "grants": grants},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests — error handling
# ---------------------------------------------------------------------------


class TestGrantErrorHandling:
    """Error cases for grant creation."""

    def test_rebac_manager_unavailable_no_key_created(self, mock_db_provider, record_store):
        """503 when rebac_manager is missing — key must NOT be created."""
        from sqlalchemy import func, select

        from nexus.server.dependencies import require_admin
        from nexus.storage.models import APIKeyModel

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_admin] = lambda: None
        app.state.auth_provider = mock_db_provider
        # Deliberately do NOT set app.state.rebac_manager

        # Count keys before
        with record_store.session_factory() as session:
            before = session.scalar(select(func.count()).select_from(APIKeyModel))

        no_rebac_client = TestClient(app)
        resp = no_rebac_client.post(
            "/api/v2/auth/keys",
            json={
                "name": "no-rebac",
                "grants": [{"path": "/foo", "role": "viewer"}],
            },
        )
        assert resp.status_code == 503
        assert "ReBAC manager not available" in resp.json()["detail"]

        # No key should have been created
        with record_store.session_factory() as session:
            after = session.scalar(select(func.count()).select_from(APIKeyModel))
        assert after == before

    def test_grant_failure_rolls_back_key(self, mock_db_provider, record_store):
        """If grant creation fails, the API key should be revoked."""
        from nexus.server.dependencies import require_admin
        from nexus.storage.models import APIKeyModel

        # Use a rebac_manager that raises on write
        broken_rebac = MagicMock()
        broken_rebac.rebac_write_batch.side_effect = RuntimeError("db exploded")

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_admin] = lambda: None
        app.state.auth_provider = mock_db_provider
        app.state.rebac_manager = broken_rebac

        err_client = TestClient(app)
        resp = err_client.post(
            "/api/v2/auth/keys",
            json={
                "name": "doomed-key",
                "user_id": "doomed-user",
                "grants": [{"path": "/foo", "role": "viewer"}],
            },
        )
        assert resp.status_code == 500
        assert "rolled back" in resp.json()["detail"]

        # Verify the key was revoked
        with record_store.session_factory() as session:
            from sqlalchemy import select

            key = session.scalar(select(APIKeyModel).where(APIKeyModel.user_id == "doomed-user"))
            assert key is not None
            assert key.revoked == 1


# ---------------------------------------------------------------------------
# Tests — revocation grant cleanup
# ---------------------------------------------------------------------------


class TestRevokeKeyGrantCleanup:
    """Revoking a key should delete its associated ReBAC grants."""

    def test_revoke_deletes_grants(self, client, rebac_manager):
        # Create a key with grants
        resp = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "eve",
                "user_id": "eve-789",
                "subject_type": "user",
                "grants": [
                    {"path": "/workspace/a", "role": "editor"},
                    {"path": "/workspace/b", "role": "viewer"},
                ],
            },
        )
        assert resp.status_code == 201
        key_id = resp.json()["key_id"]

        # Confirm grants exist
        assert rebac_manager.rebac_check(
            subject=("user", "eve-789"),
            permission="write",
            object=("file", "/workspace/a"),
        )

        # Revoke the key
        resp = client.delete(f"/api/v2/auth/keys/{key_id}")
        assert resp.status_code == 200

        # Grants should be gone
        assert not rebac_manager.rebac_check(
            subject=("user", "eve-789"),
            permission="write",
            object=("file", "/workspace/a"),
        )
        assert not rebac_manager.rebac_check(
            subject=("user", "eve-789"),
            permission="read",
            object=("file", "/workspace/b"),
        )

    def test_revoke_does_not_delete_identical_preexisting_grant(self, client, rebac_manager):
        """Exact codex repro: pre-existing grant with same (subject, relation, object)
        must survive key revocation when the key requested the same grant."""
        # Create a pre-existing direct_viewer on /same
        rebac_manager.rebac_write_batch(
            [
                {
                    "subject": ("user", "overlap-user"),
                    "relation": "direct_viewer",
                    "object": ("file", "/same"),
                    "zone_id": "root",
                }
            ]
        )
        assert rebac_manager.rebac_check(
            subject=("user", "overlap-user"),
            permission="read",
            object=("file", "/same"),
        )

        # Create a key requesting the SAME viewer grant
        resp = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "overlap-key",
                "user_id": "overlap-user",
                "subject_type": "user",
                "grants": [{"path": "/same", "role": "viewer"}],
            },
        )
        assert resp.status_code == 201
        key_id = resp.json()["key_id"]

        # Revoke the key
        resp = client.delete(f"/api/v2/auth/keys/{key_id}")
        assert resp.status_code == 200

        # The original pre-existing grant must still be alive
        assert rebac_manager.rebac_check(
            subject=("user", "overlap-user"),
            permission="read",
            object=("file", "/same"),
        )

    def test_revoke_only_deletes_own_grants(self, client, rebac_manager):
        """Revoking key A must not delete grants from key B for the same user."""
        # Create key A with grants
        resp_a = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "key-a",
                "user_id": "shared-user",
                "subject_type": "user",
                "grants": [{"path": "/project-a/*", "role": "editor"}],
            },
        )
        assert resp_a.status_code == 201
        key_a_id = resp_a.json()["key_id"]

        # Create key B with different grants for the SAME user
        resp_b = client.post(
            "/api/v2/auth/keys",
            json={
                "name": "key-b",
                "user_id": "shared-user",
                "subject_type": "user",
                "grants": [{"path": "/project-b/*", "role": "viewer"}],
            },
        )
        assert resp_b.status_code == 201

        # Also create a pre-existing grant NOT from any key
        rebac_manager.rebac_write_batch(
            [
                {
                    "subject": ("user", "shared-user"),
                    "relation": "direct_viewer",
                    "object": ("file", "/preexisting"),
                    "zone_id": "root",
                }
            ]
        )

        # Confirm all three grants exist
        assert rebac_manager.rebac_check(
            subject=("user", "shared-user"),
            permission="write",
            object=("file", "/project-a/*"),
        )
        assert rebac_manager.rebac_check(
            subject=("user", "shared-user"),
            permission="read",
            object=("file", "/project-b/*"),
        )
        assert rebac_manager.rebac_check(
            subject=("user", "shared-user"),
            permission="read",
            object=("file", "/preexisting"),
        )

        # Revoke key A only
        resp = client.delete(f"/api/v2/auth/keys/{key_a_id}")
        assert resp.status_code == 200

        # Key A's grants should be gone
        assert not rebac_manager.rebac_check(
            subject=("user", "shared-user"),
            permission="write",
            object=("file", "/project-a/*"),
        )

        # Key B's grants must still exist
        assert rebac_manager.rebac_check(
            subject=("user", "shared-user"),
            permission="read",
            object=("file", "/project-b/*"),
        )

        # Pre-existing grant must still exist
        assert rebac_manager.rebac_check(
            subject=("user", "shared-user"),
            permission="read",
            object=("file", "/preexisting"),
        )

    def test_revoke_without_grants_still_works(self, client):
        # Create a key without grants
        resp = client.post(
            "/api/v2/auth/keys",
            json={"name": "frank", "user_id": "frank-000"},
        )
        assert resp.status_code == 201
        key_id = resp.json()["key_id"]

        # Revoke should succeed normally
        resp = client.delete(f"/api/v2/auth/keys/{key_id}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True


# ---------------------------------------------------------------------------
# Tests — model unit tests
# ---------------------------------------------------------------------------


class TestGrantRequestModel:
    """Unit tests for GrantRequest Pydantic model."""

    def test_valid_grant(self):
        g = GrantRequest(path="/workspace/a", role="editor")
        assert g.path == "/workspace/a"
        assert g.role == "editor"

    def test_role_literal_constraint(self):
        with pytest.raises(ValueError):
            GrantRequest(path="/foo", role="invalid")

    def test_path_must_be_absolute(self):
        with pytest.raises(ValueError):
            GrantRequest(path="relative", role="viewer")

    def test_path_no_traversal(self):
        with pytest.raises(ValueError):
            GrantRequest(path="/a/../b", role="viewer")

    def test_role_to_relation_mapping(self):
        assert ROLE_TO_RELATION == {
            "viewer": "direct_viewer",
            "editor": "direct_editor",
            "owner": "direct_owner",
        }
