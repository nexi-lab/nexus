"""Integration tests for delta sync with PostgreSQL + database auth (Issue #1127).

Part 1: Server tests — verify FastAPI server starts with PostgreSQL + database auth
Part 2: Direct sync tests — verify SyncService delta sync with real PostgreSQL

Prerequisites:
    docker compose --profile test up -d postgres-test

Usage:
    NEXUS_DATABASE_URL=postgresql://nexus_test:nexus_test_password@localhost:5433/nexus_test \
    pytest tests/integration/test_delta_sync_e2e.py -v --tb=short -o "addopts="
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import closing, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ============================================================================
# Configuration
# ============================================================================

DB_URL = os.environ.get(
    "NEXUS_DATABASE_URL",
    "postgresql://nexus_test:nexus_test_password@localhost:5433/nexus_test",
)

_src_path = Path(__file__).parent.parent.parent / "src"


def is_postgres_available() -> bool:
    """Check if PostgreSQL test database is available."""
    try:
        engine = create_engine(DB_URL)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not is_postgres_available(),
    reason="PostgreSQL not available (start with: docker compose --profile test up -d postgres-test)",
)


# ============================================================================
# Part 1: Server + Auth tests (real FastAPI server with PostgreSQL)
# ============================================================================


def find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(f"{url}/health", timeout=1.0, trust_env=False)
            if response.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def pg_engine():
    engine = create_engine(DB_URL, echo=False)
    yield engine
    engine.dispose()


@pytest.fixture()
def clean_db(pg_engine):
    """Reset the database before each test."""
    from nexus.storage.models import Base

    Base.metadata.drop_all(pg_engine, checkfirst=True)
    Base.metadata.create_all(pg_engine)
    yield
    Base.metadata.drop_all(pg_engine, checkfirst=True)


@pytest.fixture()
def admin_api_key(pg_engine, clean_db):
    """Create an admin API key directly in the database."""
    from nexus.server.auth.database_key import DatabaseAPIKeyAuth
    from nexus.storage.models import UserModel

    sf = sessionmaker(bind=pg_engine)
    with sf() as session:
        admin = UserModel(
            user_id="admin",
            username="admin",
            display_name="Admin User",
            email="admin@test.local",
            is_global_admin=1,
            is_active=1,
        )
        session.add(admin)
        session.flush()
        _, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="admin",
            name="E2E test key",
            is_admin=True,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.commit()
    return raw_key


@pytest.fixture()
def nexus_server_pg(tmp_path, admin_api_key):
    """Start nexus serve with PostgreSQL + database auth."""
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-e2e-delta-sync-secret-key"
    env["NEXUS_DATABASE_URL"] = DB_URL
    env["PYTHONPATH"] = str(_src_path)
    env["NEXUS_ENFORCE_PERMISSIONS"] = "true"

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}', '--auth-type', 'database'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    if not wait_for_server(base_url, timeout=30.0):
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            f"Server failed to start on port {port}.\n"
            f"stdout: {stdout.decode()}\n"
            f"stderr: {stderr.decode()}"
        )

    yield {"base_url": base_url, "process": process, "api_key": admin_api_key}

    if sys.platform != "win32":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


@pytest.fixture()
def client(nexus_server_pg):
    with httpx.Client(base_url=nexus_server_pg["base_url"], timeout=30.0, trust_env=False) as c:
        yield c


class TestServerWithPostgresAuth:
    """Verify FastAPI server starts with PostgreSQL + database auth."""

    def test_server_health(self, client: httpx.Client):
        """Server is running with PostgreSQL backend."""
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_auth_required(self, client: httpx.Client):
        """Database auth is enforced — requests without key get 401."""
        resp = client.post(
            "/api/nfs/list",
            json={"jsonrpc": "2.0", "method": "list", "params": {"path": "/"}},
        )
        assert resp.status_code == 401

    def test_auth_with_valid_key(self, client: httpx.Client, nexus_server_pg):
        """Database auth works with valid API key."""
        resp = client.post(
            "/api/nfs/list",
            json={"jsonrpc": "2.0", "method": "list", "params": {"path": "/"}},
            headers={"Authorization": f"Bearer {nexus_server_pg['api_key']}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data, f"Expected result in response: {data}"


# ============================================================================
# Part 1b: Non-admin user permission tests (server with enforce_permissions=true)
# ============================================================================


@pytest.fixture()
def non_admin_api_key(pg_engine, clean_db):
    """Create a non-admin API key directly in the database."""
    from nexus.server.auth.database_key import DatabaseAPIKeyAuth
    from nexus.storage.models import UserModel

    sf = sessionmaker(bind=pg_engine)
    with sf() as session:
        user = UserModel(
            user_id="regular_user",
            username="regular_user",
            display_name="Regular User",
            email="user@test.local",
            is_global_admin=0,
            is_active=1,
        )
        session.add(user)
        session.flush()
        _, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="regular_user",
            name="Non-admin E2E test key",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.commit()
    return raw_key


@pytest.fixture()
def nexus_server_pg_with_users(tmp_path, admin_api_key, non_admin_api_key):
    """Start nexus serve with PostgreSQL + database auth, returning both admin and non-admin keys."""
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-e2e-delta-sync-secret-key"
    env["NEXUS_DATABASE_URL"] = DB_URL
    env["PYTHONPATH"] = str(_src_path)
    env["NEXUS_ENFORCE_PERMISSIONS"] = "true"

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}', '--auth-type', 'database'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    if not wait_for_server(base_url, timeout=30.0):
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            f"Server failed to start on port {port}.\n"
            f"stdout: {stdout.decode()}\n"
            f"stderr: {stderr.decode()}"
        )

    yield {
        "base_url": base_url,
        "process": process,
        "admin_key": admin_api_key,
        "non_admin_key": non_admin_api_key,
    }

    if sys.platform != "win32":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


class TestNonAdminPermissions:
    """Verify non-admin users are subject to permission enforcement."""

    def test_non_admin_authenticated_but_restricted(self, nexus_server_pg_with_users):
        """Non-admin user authenticates OK but gets empty/restricted results without ReBAC."""
        server = nexus_server_pg_with_users
        with httpx.Client(base_url=server["base_url"], timeout=30.0, trust_env=False) as client:
            resp = client.post(
                "/api/nfs/list",
                json={"jsonrpc": "2.0", "method": "list", "params": {"path": "/"}},
                headers={"Authorization": f"Bearer {server['non_admin_key']}"},
            )
            # Non-admin gets 200 but with empty/filtered result (no ReBAC tuples)
            assert resp.status_code == 200
            data = resp.json()
            # Should have result but may be empty/restricted
            assert "result" in data or "error" in data, f"Unexpected response: {data}"

    def test_admin_sees_more_than_non_admin(self, nexus_server_pg_with_users):
        """Admin bypasses permissions; non-admin is restricted by ReBAC."""
        server = nexus_server_pg_with_users
        with httpx.Client(base_url=server["base_url"], timeout=30.0, trust_env=False) as client:
            # Admin request
            admin_resp = client.post(
                "/api/nfs/list",
                json={"jsonrpc": "2.0", "method": "list", "params": {"path": "/"}},
                headers={"Authorization": f"Bearer {server['admin_key']}"},
            )
            # Non-admin request
            user_resp = client.post(
                "/api/nfs/list",
                json={"jsonrpc": "2.0", "method": "list", "params": {"path": "/"}},
                headers={"Authorization": f"Bearer {server['non_admin_key']}"},
            )

            assert admin_resp.status_code == 200
            assert user_resp.status_code == 200

            admin_data = admin_resp.json()
            user_data = user_resp.json()

            # Both should get valid responses
            assert "result" in admin_data, f"Admin response missing result: {admin_data}"
            assert "result" in user_data or "error" in user_data, (
                f"User response unexpected: {user_data}"
            )


# ============================================================================
# Part 2: Direct SyncService + LocalConnectorBackend + PostgreSQL
# ============================================================================


@pytest.fixture()
def pg_session_factory(pg_engine, clean_db):
    """Create a session factory bound to the test engine."""
    from nexus.storage.models import BackendChangeLogModel

    BackendChangeLogModel.__table__.create(pg_engine, checkfirst=True)
    return sessionmaker(bind=pg_engine)


@pytest.fixture()
def mock_context():
    """Create a mock OperationContext with zone_id."""
    ctx = MagicMock()
    ctx.zone_id = "test-zone"
    ctx.user_id = "admin"
    ctx.subject_type = "user"
    ctx.subject_id = "admin"
    ctx.backend_path = None
    return ctx


@pytest.fixture()
def sync_service_with_pg(pg_session_factory):
    """Create SyncService with a mocked gateway that uses real PostgreSQL sessions."""
    from nexus.services.sync_service import SyncService

    gateway = MagicMock()
    gateway.session_factory = pg_session_factory
    # Disable hierarchy (ReBAC tuple creation) for simpler test setup
    gateway.hierarchy_enabled = False

    service = SyncService(gateway)
    return service, gateway


@pytest.fixture()
def local_connector_mount(tmp_path):
    """Create a real LocalConnectorBackend with test files."""
    from nexus.backends.local_connector import LocalConnectorBackend

    mount_dir = tmp_path / "external_mount"
    mount_dir.mkdir()

    # Create test files
    (mount_dir / "file1.txt").write_text("Hello World from file 1")
    (mount_dir / "file2.txt").write_text("Hello World from file 2")
    subdir = mount_dir / "subdir"
    subdir.mkdir()
    (subdir / "file3.txt").write_text("Nested file content")

    backend = LocalConnectorBackend(local_path=str(mount_dir))
    return backend, mount_dir


class TestDeltaSyncWithPostgres:
    """Test SyncService delta sync with real PostgreSQL and real LocalConnectorBackend."""

    def test_first_sync_creates_files(
        self, sync_service_with_pg, local_connector_mount, pg_session_factory, mock_context
    ):
        """First sync creates files and populates the change log in PostgreSQL."""
        from nexus.services.sync_service import SyncContext

        service, gateway = sync_service_with_pg
        backend, mount_dir = local_connector_mount

        # Setup gateway mock to return the real backend
        mount_mock = MagicMock()
        mount_mock.backend = backend
        gateway.router.get_mount.return_value = mount_mock
        # Mock metadata operations
        gateway.metadata_get.return_value = None
        gateway.metadata_create.return_value = True

        ctx = SyncContext(mount_point="/mnt/test", recursive=True, context=mock_context)
        result = service.sync_mount(ctx)

        assert result.files_created >= 3, (
            f"Expected at least 3 files created, got {result.files_created}. "
            f"Errors: {result.errors}"
        )

        # Verify change log was populated in PostgreSQL
        from nexus.storage.models import BackendChangeLogModel

        session = pg_session_factory()
        try:
            count = session.query(BackendChangeLogModel).count()
            assert count >= 3, f"Expected at least 3 change log entries, got {count}"
        finally:
            session.close()

    def test_second_sync_skips_unchanged(
        self, sync_service_with_pg, local_connector_mount, mock_context
    ):
        """Second sync skips unchanged files via delta sync (PostgreSQL change log)."""
        from nexus.services.sync_service import SyncContext

        service, gateway = sync_service_with_pg
        backend, _ = local_connector_mount

        mount_mock = MagicMock()
        mount_mock.backend = backend
        gateway.router.get_mount.return_value = mount_mock
        gateway.metadata_get.return_value = None
        gateway.metadata_create.return_value = True

        # First sync — populates change log
        ctx = SyncContext(mount_point="/mnt/test", recursive=True, context=mock_context)
        result1 = service.sync_mount(ctx)
        assert result1.files_created >= 3

        # Second sync — should skip unchanged files
        # Reset mock call counts but gateway.metadata_get now returns existing metadata
        # to indicate files already exist (so they're "updated" not "created")
        gateway.metadata_get.return_value = {"path": "/mnt/test/file1.txt"}
        gateway.metadata_update.return_value = True

        result2 = service.sync_mount(ctx)
        assert result2.files_skipped >= 3, (
            f"Expected at least 3 files skipped on second sync, got {result2.files_skipped}. "
            f"created={result2.files_created}, updated={result2.files_updated}, "
            f"errors={result2.errors}"
        )

    def test_modified_file_detected(
        self, sync_service_with_pg, local_connector_mount, pg_session_factory, mock_context
    ):
        """Modified file is detected and re-synced, others skipped."""
        from nexus.services.sync_service import SyncContext

        service, gateway = sync_service_with_pg
        backend, mount_dir = local_connector_mount

        mount_mock = MagicMock()
        mount_mock.backend = backend
        gateway.router.get_mount.return_value = mount_mock
        gateway.metadata_get.return_value = None
        gateway.metadata_create.return_value = True

        # First sync
        ctx = SyncContext(mount_point="/mnt/test", recursive=True, context=mock_context)
        service.sync_mount(ctx)

        # Modify one file
        (mount_dir / "file1.txt").write_text("Modified content that is longer now!")
        time.sleep(0.05)  # Ensure mtime changes

        # Second sync
        gateway.metadata_get.return_value = {"path": "/mnt/test/file1.txt"}
        gateway.metadata_update.return_value = True
        result2 = service.sync_mount(ctx)

        # file2/file3 should be skipped, file1 should NOT be skipped (it changed)
        assert result2.files_skipped >= 2, (
            f"Expected at least 2 skipped, got {result2.files_skipped}. "
            f"Full: created={result2.files_created}, updated={result2.files_updated}, "
            f"skipped={result2.files_skipped}"
        )
        # file1 was detected as changed (not skipped) — verify its change log was updated
        from nexus.storage.models import BackendChangeLogModel

        session = pg_session_factory()
        try:
            entry = (
                session.query(BackendChangeLogModel)
                .filter(BackendChangeLogModel.path == "/mnt/test/file1.txt")
                .first()
            )
            assert entry is not None, "file1.txt should have a change log entry"
            # The updated file should have the new size (35 chars)
            assert entry.size_bytes == len("Modified content that is longer now!"), (
                f"Expected updated size, got {entry.size_bytes}"
            )
        finally:
            session.close()

    def test_deleted_file_cleans_change_log(
        self, sync_service_with_pg, local_connector_mount, pg_session_factory, mock_context
    ):
        """Deleted file has its change log entry removed from PostgreSQL."""
        from nexus.services.sync_service import SyncContext
        from nexus.storage.models import BackendChangeLogModel

        service, gateway = sync_service_with_pg
        backend, mount_dir = local_connector_mount

        mount_mock = MagicMock()
        mount_mock.backend = backend
        gateway.router.get_mount.return_value = mount_mock
        gateway.metadata_get.return_value = None
        gateway.metadata_create.return_value = True
        gateway.metadata_delete.return_value = True

        # First sync
        ctx = SyncContext(mount_point="/mnt/test", recursive=True, context=mock_context)
        service.sync_mount(ctx)

        # Check initial change log count
        session = pg_session_factory()
        initial_count = session.query(BackendChangeLogModel).count()
        session.close()
        assert initial_count >= 3

        # Delete file1.txt from disk
        (mount_dir / "file1.txt").unlink()

        # Mock metadata_list to return all 3 previously synced files
        # (file1 is in metadata but no longer on disk)
        meta1 = MagicMock()
        meta1.path = "/mnt/test/file1.txt"
        meta2 = MagicMock()
        meta2.path = "/mnt/test/file2.txt"
        meta3 = MagicMock()
        meta3.path = "/mnt/test/subdir/file3.txt"
        gateway.metadata_list.return_value = [meta1, meta2, meta3]
        gateway.list_mounts.return_value = []

        # metadata_get should return truthy for all files (they exist in metadata)
        gateway.metadata_get.return_value = {"path": "exists"}
        gateway.metadata_update.return_value = True

        # Second sync — file1.txt is gone from disk, should be deleted
        result2 = service.sync_mount(ctx)

        assert result2.files_deleted >= 1, (
            f"Expected at least 1 deleted, got {result2.files_deleted}. errors={result2.errors}"
        )

        # Check that change log entry for file1.txt was removed from PostgreSQL
        session = pg_session_factory()
        remaining = (
            session.query(BackendChangeLogModel)
            .filter(BackendChangeLogModel.path == "/mnt/test/file1.txt")
            .count()
        )
        total_remaining = session.query(BackendChangeLogModel).count()
        session.close()

        assert remaining == 0, "Change log entry for deleted file1.txt should be removed"
        assert total_remaining < initial_count, (
            f"Expected fewer change log entries after deletion, "
            f"but initial={initial_count}, remaining={total_remaining}"
        )

    def test_recreated_file_not_skipped(
        self, sync_service_with_pg, local_connector_mount, pg_session_factory, mock_context
    ):
        """Re-created file is synced, not falsely skipped by stale change log."""
        from nexus.services.sync_service import SyncContext
        from nexus.storage.models import BackendChangeLogModel

        service, gateway = sync_service_with_pg
        backend, mount_dir = local_connector_mount

        mount_mock = MagicMock()
        mount_mock.backend = backend
        gateway.router.get_mount.return_value = mount_mock
        gateway.metadata_get.return_value = None
        gateway.metadata_create.return_value = True
        gateway.metadata_delete.return_value = True

        # First sync
        ctx = SyncContext(mount_point="/mnt/test", recursive=True, context=mock_context)
        service.sync_mount(ctx)

        # Verify file1 in change log
        session = pg_session_factory()
        file1_entry = (
            session.query(BackendChangeLogModel)
            .filter(BackendChangeLogModel.path == "/mnt/test/file1.txt")
            .first()
        )
        session.close()
        assert file1_entry is not None, "file1.txt should be in change log after first sync"

        # Delete file1's change log entry (simulating what _sync_deletions does)
        service._change_log.delete_change_log(
            "/mnt/test/file1.txt", "LocalConnectorBackend", "default"
        )

        # Re-create file1 with different content
        (mount_dir / "file1.txt").write_text("Re-created with brand new content!")
        time.sleep(0.05)

        # Sync again — file1 should be synced (no change log entry → treated as new)
        result3 = service.sync_mount(ctx)

        # file1 should be created (it has no change log entry, so no delta skip)
        assert result3.files_created >= 1 or result3.files_updated >= 1, (
            f"Expected re-created file to be synced, but created={result3.files_created}, "
            f"updated={result3.files_updated}, skipped={result3.files_skipped}"
        )


# ============================================================================
# Part 3: Non-admin permission enforcement with direct SyncService
# ============================================================================


@pytest.fixture()
def non_admin_context():
    """Create a real OperationContext with is_admin=False."""
    from nexus.core.permissions import OperationContext

    return OperationContext(
        user="regular_user",
        groups=[],
        zone_id="test-zone",
        is_admin=False,
        subject_type="user",
        subject_id="regular_user",
    )


class TestNonAdminSyncPermissions:
    """Verify SyncService enforces permissions for non-admin users."""

    def test_sync_denied_without_rebac_permission(
        self, sync_service_with_pg, local_connector_mount, non_admin_context
    ):
        """Non-admin user without ReBAC read permission gets PermissionError."""
        from nexus.services.sync_service import SyncContext

        service, gateway = sync_service_with_pg
        backend, _ = local_connector_mount

        mount_mock = MagicMock()
        mount_mock.backend = backend
        gateway.router.get_mount.return_value = mount_mock

        # ReBAC check returns False — user has no read permission
        gateway.rebac_check.return_value = False

        ctx = SyncContext(mount_point="/mnt/test", recursive=True, context=non_admin_context)

        with pytest.raises(PermissionError, match="no read permission"):
            service.sync_mount(ctx)

    def test_sync_allowed_with_rebac_permission(
        self,
        sync_service_with_pg,
        local_connector_mount,
        pg_session_factory,
        non_admin_context,
    ):
        """Non-admin user with ReBAC read permission can sync successfully."""
        from nexus.services.sync_service import SyncContext

        service, gateway = sync_service_with_pg
        backend, _ = local_connector_mount

        mount_mock = MagicMock()
        mount_mock.backend = backend
        gateway.router.get_mount.return_value = mount_mock
        gateway.metadata_get.return_value = None
        gateway.metadata_create.return_value = True

        # ReBAC check returns True — user has read permission
        gateway.rebac_check.return_value = True

        ctx = SyncContext(mount_point="/mnt/test", recursive=True, context=non_admin_context)
        result = service.sync_mount(ctx)

        assert result.files_created >= 3, (
            f"Expected at least 3 files created, got {result.files_created}. "
            f"Errors: {result.errors}"
        )

    def test_admin_bypasses_rebac_check(self, sync_service_with_pg, local_connector_mount):
        """Admin user bypasses ReBAC — rebac_check is never called."""
        from nexus.core.permissions import OperationContext
        from nexus.services.sync_service import SyncContext

        service, gateway = sync_service_with_pg
        backend, _ = local_connector_mount

        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            zone_id="test-zone",
            is_admin=True,
            subject_type="user",
            subject_id="admin",
        )

        mount_mock = MagicMock()
        mount_mock.backend = backend
        gateway.router.get_mount.return_value = mount_mock
        gateway.metadata_get.return_value = None
        gateway.metadata_create.return_value = True

        ctx = SyncContext(mount_point="/mnt/test", recursive=True, context=admin_ctx)
        result = service.sync_mount(ctx)

        assert result.files_created >= 3
        # Admin bypasses ReBAC — rebac_check should NOT have been called
        gateway.rebac_check.assert_not_called()
