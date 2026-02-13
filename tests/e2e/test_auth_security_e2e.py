"""E2E tests for authentication and security flows.

Covers:
A) API key lifecycle — create, authenticate, expire, revoke
B) Cross-zone isolation — read/write/list denied across zone boundaries
C) Permission enforcement — unauthenticated, read-only, write-denied
D) Rate limiting — verify 429 when exceeding anonymous limit

Uses Starlette TestClient with real NexusFS + RaftMetadataStore
(same pattern as test_path_unscoping_e2e.py).

Run with:
    pytest tests/e2e/test_auth_security_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS
from nexus.core.permissions import OperationContext
from nexus.factory import create_nexus_fs
from nexus.raft import _HAS_METASTORE
from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.storage.models import Base
from nexus.storage.record_store import SQLAlchemyRecordStore

# Skip entire module if native Metastore is not built
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _HAS_METASTORE,
        reason="Requires native _nexus_raft module (maturin develop)",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rpc_body(method: str, params: dict | None = None) -> str:
    """Build JSON-RPC request body."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or {},
        }
    )


def _rpc_post(
    client: TestClient,
    method: str,
    params: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, dict]:
    """Make RPC call, return (status_code, parsed_json)."""
    default_headers = {"Content-Type": "application/json"}
    if headers:
        default_headers.update(headers)
    resp = client.post(
        f"/api/nfs/{method}",
        content=_rpc_body(method, params),
        headers=default_headers,
    )
    return resp.status_code, resp.json()


def _create_nexus_fs(
    tmp_path: Path,
    *,
    enforce_permissions: bool = False,
    suffix: str = "",
) -> NexusFS:
    """Create a real NexusFS with RaftMetadataStore and optional permission enforcement."""
    from nexus.storage.raft_metadata_store import RaftMetadataStore

    storage_path = tmp_path / f"storage{suffix}"
    storage_path.mkdir(exist_ok=True)
    backend = LocalBackend(root_path=storage_path)

    raft_dir = str(tmp_path / f"raft-metadata{suffix}")
    metadata_store = RaftMetadataStore.embedded(raft_dir)

    record_store = SQLAlchemyRecordStore(db_url=f"sqlite:///{tmp_path / f'records{suffix}.db'}")

    return create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        enforce_permissions=enforce_permissions,
        is_admin=False,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _db_engine(tmp_path: Path):
    """Create a SQLite engine with all tables for auth tests."""
    db_path = tmp_path / "auth_security.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def _session_factory(_db_engine):
    """SQLAlchemy session factory bound to the test database."""
    return sessionmaker(bind=_db_engine)


@pytest.fixture()
def nexus_fs_enforced(tmp_path: Path):
    """NexusFS with enforce_permissions=True and services (ReBAC, etc.)."""
    nx = _create_nexus_fs(tmp_path, enforce_permissions=True, suffix="_enforced")
    yield nx
    nx.close()


@pytest.fixture()
def nexus_fs_open(tmp_path: Path):
    """NexusFS with enforce_permissions=False (for writing seed data)."""
    nx = _create_nexus_fs(tmp_path, enforce_permissions=False, suffix="_open")
    yield nx
    nx.close()


def _make_app_with_db_auth(
    nexus_fs: NexusFS,
    session_factory,
    tmp_path: Path,
    monkeypatch,
):
    """Create a FastAPI app backed by DatabaseAPIKeyAuth."""
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "false")
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")

    from nexus.server.fastapi_server import create_app

    auth_provider = DatabaseAPIKeyAuth(session_factory)
    db_url = f"sqlite:///{tmp_path / 'records_open.db'}"
    return create_app(
        nexus_fs=nexus_fs,
        auth_provider=auth_provider,
        database_url=db_url,
    )


def _make_app_with_static_key(
    nexus_fs: NexusFS,
    api_key: str,
    tmp_path: Path,
    monkeypatch,
    *,
    rate_limit: bool = False,
):
    """Create a FastAPI app with static API key auth."""
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")
    if rate_limit:
        monkeypatch.setenv("NEXUS_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("NEXUS_RATE_LIMIT_ANONYMOUS", "5/minute")
        monkeypatch.setenv("NEXUS_RATE_LIMIT_AUTHENTICATED", "5/minute")
        # Patch module-level constants (read at import time)
        import nexus.server.rate_limiting as _rl

        monkeypatch.setattr(_rl, "RATE_LIMIT_ANONYMOUS", "5/minute")
        monkeypatch.setattr(_rl, "RATE_LIMIT_AUTHENTICATED", "5/minute")
    else:
        monkeypatch.delenv("NEXUS_RATE_LIMIT_ENABLED", raising=False)

    from nexus.server.fastapi_server import create_app

    db_url = f"sqlite:///{tmp_path / 'records_open.db'}"
    return create_app(
        nexus_fs=nexus_fs,
        api_key=api_key,
        database_url=db_url,
    )


# ---------------------------------------------------------------------------
# A) API Key Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestAPIKeyLifecycle:
    """Verify create / authenticate / expire / revoke lifecycle for DB API keys."""

    def test_valid_api_key_authenticates(
        self, tmp_path: Path, nexus_fs_open: NexusFS, _session_factory, monkeypatch
    ):
        """Create a key, send a request with it, expect 200."""
        nexus_fs_open.write("/hello.txt", b"world")

        with _session_factory() as session:
            _key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="alice",
                name="alice-laptop",
                zone_id="default",
                is_admin=True,
            )
            session.commit()

        app = _make_app_with_db_auth(nexus_fs_open, _session_factory, tmp_path, monkeypatch)
        with TestClient(app, raise_server_exceptions=False) as client:
            status, body = _rpc_post(
                client,
                "read",
                {"path": "/hello.txt"},
                headers={"Authorization": f"Bearer {raw_key}"},
            )

        assert status == 200, f"Expected 200, got {status}: {body}"
        assert "result" in body, f"Expected result in response: {body}"

    def test_invalid_api_key_rejected(
        self, tmp_path: Path, nexus_fs_open: NexusFS, _session_factory, monkeypatch
    ):
        """A fabricated key must be rejected with 401."""
        app = _make_app_with_db_auth(nexus_fs_open, _session_factory, tmp_path, monkeypatch)
        with TestClient(app, raise_server_exceptions=False) as client:
            status, body = _rpc_post(
                client,
                "list",
                {"path": "/"},
                headers={"Authorization": "Bearer sk-bogus_fake_aaaabbbb_ccccddddeeeeffffgggg"},
            )

        assert status == 401, f"Expected 401, got {status}: {body}"

    def test_expired_api_key_rejected(
        self, tmp_path: Path, nexus_fs_open: NexusFS, _session_factory, monkeypatch
    ):
        """A key whose expires_at is in the past must be rejected with 401."""
        with _session_factory() as session:
            _key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="bob",
                name="bob-expired",
                zone_id="default",
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
            session.commit()

        app = _make_app_with_db_auth(nexus_fs_open, _session_factory, tmp_path, monkeypatch)
        with TestClient(app, raise_server_exceptions=False) as client:
            status, body = _rpc_post(
                client,
                "list",
                {"path": "/"},
                headers={"Authorization": f"Bearer {raw_key}"},
            )

        assert status == 401, f"Expected 401 for expired key, got {status}: {body}"

    def test_revoked_api_key_rejected(
        self, tmp_path: Path, nexus_fs_open: NexusFS, _session_factory, monkeypatch
    ):
        """A key that has been revoked must be rejected with 401."""
        with _session_factory() as session:
            key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="carol",
                name="carol-revoked",
                zone_id="default",
            )
            session.commit()

        # Revoke the key
        with _session_factory() as session:
            revoked = DatabaseAPIKeyAuth.revoke_key(session, key_id)
            session.commit()
            assert revoked, "Revocation should return True"

        app = _make_app_with_db_auth(nexus_fs_open, _session_factory, tmp_path, monkeypatch)
        with TestClient(app, raise_server_exceptions=False) as client:
            status, body = _rpc_post(
                client,
                "list",
                {"path": "/"},
                headers={"Authorization": f"Bearer {raw_key}"},
            )

        assert status == 401, f"Expected 401 for revoked key, got {status}: {body}"


# ---------------------------------------------------------------------------
# B) Zone Isolation
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestZoneIsolation:
    """Cross-zone operations must be denied unless the caller has MANAGE_ZONES."""

    @staticmethod
    def _admin_ctx(zone_id: str) -> OperationContext:
        """Admin context for a given zone (no MANAGE_ZONES capability)."""
        return OperationContext(
            user="admin",
            groups=["admins"],
            zone_id=zone_id,
            is_admin=True,
        )

    def test_user_cannot_read_other_zone_files(self, nexus_fs_enforced: NexusFS):
        """Write file in zone A path, read from zone B context => denied."""
        nx = nexus_fs_enforced
        zone_a_path = "/zone/zone_a/user:admin/secret.txt"

        # Write as admin in zone_a
        admin_a = self._admin_ctx("zone_a")
        nx.write(zone_a_path, b"top secret", context=admin_a)

        # Attempt read from zone_b context (admin but NO MANAGE_ZONES capability)
        admin_b = self._admin_ctx("zone_b")
        with pytest.raises(PermissionError, match="[Cc]ross.zone"):
            nx.read(zone_a_path, context=admin_b)

    def test_user_cannot_list_other_zone_files(self, nexus_fs_enforced: NexusFS):
        """Listing zone A prefix from zone B context must return empty (no visibility)."""
        nx = nexus_fs_enforced
        admin_a = self._admin_ctx("zone_a")
        nx.write("/zone/zone_a/user:admin/data.csv", b"a,b,c", context=admin_a)

        # List from zone_b context at the zone_a prefix — returns empty, no cross-zone visibility
        admin_b = self._admin_ctx("zone_b")
        results = nx.list("/zone/zone_a/", context=admin_b)
        assert len(results) == 0, "Cross-zone list should return no results"

    def test_cross_zone_write_denied(self, nexus_fs_enforced: NexusFS):
        """Writing to zone A path from zone B context must be denied."""
        nx = nexus_fs_enforced
        admin_b = self._admin_ctx("zone_b")

        with pytest.raises(PermissionError, match="[Cc]ross.zone"):
            nx.write(
                "/zone/zone_a/user:admin/injected.txt",
                b"evil",
                context=admin_b,
            )


# ---------------------------------------------------------------------------
# C) Permission Enforcement
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestPermissionEnforcement:
    """Permission enforcement at HTTP and NexusFS levels."""

    def test_unauthenticated_request_denied(
        self, tmp_path: Path, nexus_fs_open: NexusFS, monkeypatch
    ):
        """A request with no Authorization header must get 401."""
        api_key = "test-static-key-for-auth-12345"
        app = _make_app_with_static_key(nexus_fs_open, api_key, tmp_path, monkeypatch)
        with TestClient(app, raise_server_exceptions=False) as client:
            status, body = _rpc_post(
                client,
                "list",
                {"path": "/"},
                # No Authorization header provided
            )

        assert status == 401, f"Expected 401, got {status}: {body}"

    def test_read_requires_permission(self, nexus_fs_enforced: NexusFS):
        """An unprivileged user cannot read a file they have no grant for."""
        nx = nexus_fs_enforced
        admin_ctx = OperationContext(
            user="admin",
            groups=["admins"],
            zone_id="default",
            is_admin=True,
        )
        nx.write("/workspace/confidential.txt", b"secret stuff", context=admin_ctx)

        # Unprivileged user with no grants
        user_ctx = OperationContext(
            user="mallory",
            groups=[],
            zone_id="default",
        )
        with pytest.raises(PermissionError, match="[Aa]ccess denied"):
            nx.read("/workspace/confidential.txt", context=user_ctx)

    def test_write_requires_permission(self, nexus_fs_enforced: NexusFS):
        """A viewer (read-only grant) cannot write to a file."""
        nx = nexus_fs_enforced
        file_path = "/zone/default/user:admin/readme.md"

        admin_ctx = OperationContext(
            user="admin",
            groups=["admins"],
            zone_id="default",
            is_admin=True,
        )
        nx.write(file_path, b"# readme", context=admin_ctx)

        # Create a read-only grant for "viewer" via ReBAC
        if nx._rebac_manager:
            nx._rebac_manager.rebac_write(
                subject=("user", "viewer"),
                relation="direct_viewer",
                object=("file", file_path),
                zone_id="default",
            )

        viewer_ctx = OperationContext(
            user="viewer",
            groups=[],
            zone_id="default",
        )

        # Read should succeed (viewer has read permission)
        content = nx.read(file_path, context=viewer_ctx)
        assert content == b"# readme"

        # Write should fail (viewer has no write permission)
        with pytest.raises(PermissionError, match="[Aa]ccess denied"):
            nx.write(file_path, b"overwritten!", context=viewer_ctx)

    def test_edit_denied_without_permission(self, nexus_fs_enforced: NexusFS):
        """An unprivileged user cannot edit a file they have no grant for."""
        nx = nexus_fs_enforced
        admin_ctx = OperationContext(
            user="admin",
            groups=["admins"],
            zone_id="default",
            is_admin=True,
        )
        file_path = "/workspace/protected.py"
        nx.write(file_path, b"def foo():\n    return 1\n", context=admin_ctx)

        # Unprivileged user with no grants
        user_ctx = OperationContext(
            user="mallory",
            groups=[],
            zone_id="default",
        )
        with pytest.raises(PermissionError, match="[Aa]ccess denied"):
            nx.edit(
                file_path,
                [{"old_str": "foo", "new_str": "bar"}],
                context=user_ctx,
            )

    def test_edit_requires_write_permission(self, nexus_fs_enforced: NexusFS):
        """A viewer (read-only grant) cannot edit a file — edit needs write permission."""
        nx = nexus_fs_enforced
        file_path = "/zone/default/user:admin/editable.py"

        admin_ctx = OperationContext(
            user="admin",
            groups=["admins"],
            zone_id="default",
            is_admin=True,
        )
        nx.write(file_path, b"def hello():\n    return 'world'\n", context=admin_ctx)

        # Create a read-only grant for "viewer" via ReBAC
        if nx._rebac_manager:
            nx._rebac_manager.rebac_write(
                subject=("user", "viewer"),
                relation="direct_viewer",
                object=("file", file_path),
                zone_id="default",
            )

        viewer_ctx = OperationContext(
            user="viewer",
            groups=[],
            zone_id="default",
        )

        # Read should succeed (viewer has read permission)
        content = nx.read(file_path, context=viewer_ctx)
        assert content == b"def hello():\n    return 'world'\n"

        # Edit should fail — edit delegates to write(), viewer has no write permission
        with pytest.raises(PermissionError, match="[Aa]ccess denied"):
            nx.edit(
                file_path,
                [{"old_str": "hello", "new_str": "goodbye"}],
                context=viewer_ctx,
            )

        # Verify file unchanged
        content_after = nx.read(file_path, context=viewer_ctx)
        assert content_after == b"def hello():\n    return 'world'\n"

    def test_edit_succeeds_with_write_permission(self, nexus_fs_enforced: NexusFS):
        """A user with write permission can successfully edit a file."""
        nx = nexus_fs_enforced
        file_path = "/zone/default/user:admin/writable.py"

        admin_ctx = OperationContext(
            user="admin",
            groups=["admins"],
            zone_id="default",
            is_admin=True,
        )
        nx.write(file_path, b"x = 1\n", context=admin_ctx)

        # Create a write grant for "editor" via ReBAC
        if nx._rebac_manager:
            nx._rebac_manager.rebac_write(
                subject=("user", "editor"),
                relation="direct_editor",
                object=("file", file_path),
                zone_id="default",
            )

        editor_ctx = OperationContext(
            user="editor",
            groups=[],
            zone_id="default",
        )

        # Edit should succeed (editor has write permission)
        result = nx.edit(
            file_path,
            [{"old_str": "x = 1", "new_str": "x = 42"}],
            context=editor_ctx,
        )
        assert result["success"] is True
        assert result["applied_count"] == 1

        # Verify content changed
        content = nx.read(file_path, context=editor_ctx)
        assert content == b"x = 42\n"


# ---------------------------------------------------------------------------
# D) Rate Limiting
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skip(
    reason="SlowAPI rate limiter does not enforce limits with Starlette TestClient (in-memory transport). Requires real HTTP server."
)
class TestRateLimiting:
    """Verify the server returns 429 when rate limit is exceeded."""

    def test_rate_limit_enforced(self, tmp_path: Path, nexus_fs_open: NexusFS, monkeypatch):
        """Send more requests than the anonymous limit (5/minute), expect 429.

        Uses a very low rate limit so the test triggers quickly.
        Health endpoint is limiter-exempt, so we target the RPC endpoint
        without auth (which triggers the 401 path, but the rate limiter
        fires before authentication for default limits).
        """
        api_key = "test-rate-limit-key-for-e2e-12345"
        app = _make_app_with_static_key(
            nexus_fs_open,
            api_key,
            tmp_path,
            monkeypatch,
            rate_limit=True,
        )

        got_429 = False
        with TestClient(app, raise_server_exceptions=False) as client:
            for _ in range(30):
                # Send unauthenticated RPC request -- rate limiter uses IP key.
                # The request will get 401 (no auth), but the limiter still
                # counts it. After the limit, the response becomes 429.
                status, body = _rpc_post(
                    client,
                    "list",
                    {"path": "/"},
                )
                if status == 429:
                    got_429 = True
                    assert "error" in body or "detail" in body, (
                        f"429 response missing error info: {body}"
                    )
                    break

        assert got_429, (
            "Expected at least one 429 response after exceeding the rate limit. "
            "Rate limiter may not be enforcing the configured 5/minute limit."
        )
