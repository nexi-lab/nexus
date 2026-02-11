"""End-to-end tests for SqlMetadataStore as SSOT (Issue #1246, Phase 4).

Tests the full NexusFS stack with use_sql_metadata=True:
- SQL is the single source of truth for file metadata
- ReBAC permissions enforced for non-admin users
- Full CRUD: write, read, delete, rename, list, exists
- Version history recorded directly in SQL (no RecordStoreSyncer)
- Reconciler detects drift between SQL and redb cache

Run with:
    .venv/bin/python3.12 -m pytest tests/e2e/test_sql_metadata_e2e.py -v -p no:xdist -o "addopts="
"""

from __future__ import annotations

import os
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Add src to path for local development
_src_path = Path(__file__).parent.parent.parent / "src"
if str(_src_path) not in sys.path:
    sys.path.insert(0, str(_src_path))

from nexus.backends.local import LocalBackend  # noqa: E402
from nexus.core.permissions import OperationContext  # noqa: E402
from nexus.factory import create_nexus_fs  # noqa: E402
from nexus.storage.raft_metadata_store import RaftMetadataStore  # noqa: E402
from nexus.storage.record_store import SQLAlchemyRecordStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sql_nexus(tmp_path):
    """Create NexusFS with use_sql_metadata=True and enforce_permissions=True."""
    os.environ["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"

    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)
    backend = LocalBackend(root_path=str(storage_path))

    db_path = tmp_path / "metadata.db"

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        nx = create_nexus_fs(
            backend=backend,
            metadata_store=RaftMetadataStore.embedded(str(tmp_path / "raft")),
            record_store=SQLAlchemyRecordStore(db_path=str(db_path)),
            enforce_permissions=True,
            allow_admin_bypass=True,  # Admin context bypasses checks
            use_sql_metadata=True,
            is_admin=False,  # Instance NOT admin — per-operation context decides
        )

    yield nx
    nx.close()


@pytest.fixture
def admin_ctx():
    """Admin/system context — bypasses all permission checks.

    Uses is_system=True because the kernel's exists() method calls
    _permission_enforcer.check() directly (not _check_permission()),
    which doesn't have the admin bypass shortcut.
    """
    return OperationContext(
        user="admin",
        groups=["admins"],
        zone_id="default",
        is_admin=True,
        is_system=True,
    )


@pytest.fixture
def alice_ctx():
    """Non-admin user context for alice."""
    return OperationContext(
        user="alice",
        groups=["developers"],
        zone_id="default",
        is_admin=False,
    )


@pytest.fixture
def bob_ctx():
    """Non-admin user context for bob."""
    return OperationContext(
        user="bob",
        groups=["viewers"],
        zone_id="default",
        is_admin=False,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _grant_permission(
    nx,
    subject_id: str,
    relation: str,
    path: str,
    zone_id: str = "default",
):
    """Grant a ReBAC permission tuple."""
    nx.rebac_create(
        subject=("user", subject_id),
        relation=relation,
        object=("file", path),
        zone_id=zone_id,
    )


# ---------------------------------------------------------------------------
# Test: SQL SSOT with admin context
# ---------------------------------------------------------------------------


class TestSqlSSOTBasicCRUD:
    """Test that SQL is the source of truth — basic CRUD with admin context."""

    def test_write_and_read(self, sql_nexus, admin_ctx):
        """Admin can write and read back content via SQL SSOT."""
        nx = sql_nexus
        path = "/test/hello.txt"
        content = "Hello from SQL SSOT!"

        nx.write(path, content, context=admin_ctx)
        result = nx.read(path, context=admin_ctx)

        # read() may return bytes or str depending on backend
        if isinstance(result, bytes):
            result = result.decode("utf-8")
        assert result == content

    def test_write_creates_version_history(self, sql_nexus, admin_ctx):
        """Write should create version history directly in SQL."""
        from sqlalchemy import text

        nx = sql_nexus
        path = "/test/versioned.txt"

        nx.write(path, "v1 content", context=admin_ctx)
        nx.write(path, "v2 content", context=admin_ctx)

        # Query version history via the path_id join
        store = nx.metadata
        if hasattr(store, "_session_factory"):
            with store._session_factory() as session:
                rows = session.execute(
                    text(
                        "SELECT vh.version_number, vh.source_type "
                        "FROM version_history vh "
                        "JOIN file_paths fp ON vh.resource_id = fp.path_id "
                        "WHERE fp.virtual_path = :path "
                        "ORDER BY vh.version_number"
                    ),
                    {"path": path},
                ).fetchall()
                assert len(rows) >= 2, f"Expected >=2 version records, got {len(rows)}"
                # First should be 'original', second should be update (None source_type)
                assert rows[0][0] == 1  # version_number 1
                assert rows[1][0] == 2  # version_number 2

    def test_delete(self, sql_nexus, admin_ctx):
        """Delete soft-deletes in SQL."""
        nx = sql_nexus
        path = "/test/deleteme.txt"

        nx.write(path, "temporary", context=admin_ctx)
        assert nx.exists(path, context=admin_ctx)

        nx.delete(path, context=admin_ctx)
        assert not nx.exists(path, context=admin_ctx)

    def test_list(self, sql_nexus, admin_ctx):
        """List returns files from SQL SSOT."""
        nx = sql_nexus

        nx.write("/project/a.txt", "aaa", context=admin_ctx)
        nx.write("/project/b.txt", "bbb", context=admin_ctx)
        nx.write("/other/c.txt", "ccc", context=admin_ctx)

        items = nx.list("/project/", context=admin_ctx)
        # list() may return dicts, FileMetadata objects, or strings depending on context
        names = []
        for item in items:
            if isinstance(item, dict):
                names.append(item.get("name", item.get("path", "")))
            elif isinstance(item, str):
                names.append(item)
            else:
                names.append(getattr(item, "path", str(item)))
        assert len(names) == 2

    def test_rename(self, sql_nexus, admin_ctx):
        """Rename updates the path in SQL."""
        nx = sql_nexus

        nx.write("/old/name.txt", "content", context=admin_ctx)
        nx.rename("/old/name.txt", "/new/name.txt", context=admin_ctx)

        assert not nx.exists("/old/name.txt", context=admin_ctx)
        assert nx.exists("/new/name.txt", context=admin_ctx)
        result = nx.read("/new/name.txt", context=admin_ctx)
        if isinstance(result, bytes):
            result = result.decode("utf-8")
        assert result == "content"

    def test_exists(self, sql_nexus, admin_ctx):
        """Exists checks SQL SSOT."""
        nx = sql_nexus
        assert not nx.exists("/nonexistent.txt", context=admin_ctx)

        nx.write("/existing.txt", "yes", context=admin_ctx)
        assert nx.exists("/existing.txt", context=admin_ctx)


# ---------------------------------------------------------------------------
# Test: Non-admin users with ReBAC permissions
# ---------------------------------------------------------------------------


class TestNonAdminPermissions:
    """Test that non-admin users are subject to ReBAC permission enforcement."""

    def test_write_denied_without_grant(self, sql_nexus, alice_ctx):
        """Alice cannot write without a write grant."""
        nx = sql_nexus

        with pytest.raises((PermissionError, Exception)) as exc_info:
            nx.write("/private/secret.txt", "forbidden", context=alice_ctx)

        # Should be a permission error
        err_msg = str(exc_info.value).lower()
        assert "permission" in err_msg or "denied" in err_msg or "forbidden" in err_msg

    def test_read_denied_without_grant(self, sql_nexus, alice_ctx, admin_ctx):
        """Alice cannot read a file she has no permission to."""
        nx = sql_nexus

        # Admin writes the file
        nx.write("/private/secret.txt", "top secret", context=admin_ctx)

        # Alice tries to read — should be denied
        with pytest.raises((PermissionError, FileNotFoundError, Exception)) as exc_info:
            nx.read("/private/secret.txt", context=alice_ctx)

        err_msg = str(exc_info.value).lower()
        assert "permission" in err_msg or "denied" in err_msg or "not found" in err_msg

    def test_write_allowed_with_grant(self, sql_nexus, alice_ctx, admin_ctx):
        """Alice can write after being granted writer relation."""
        nx = sql_nexus

        # Grant alice writer on /alice/ directory
        _grant_permission(nx, "alice", "direct_editor", "/alice/")
        _grant_permission(nx, "alice", "direct_editor", "/alice/doc.txt")

        # Alice writes
        nx.write("/alice/doc.txt", "alice's content", context=alice_ctx)

        # Admin reads back — should match
        result = nx.read("/alice/doc.txt", context=admin_ctx)
        if isinstance(result, bytes):
            result = result.decode("utf-8")
        assert result == "alice's content"

    def test_read_allowed_with_grant(self, sql_nexus, alice_ctx, admin_ctx):
        """Alice can read after being granted viewer relation."""
        nx = sql_nexus

        # Admin writes (use path under root to avoid namespace/zone parsing)
        nx.write("/readme.txt", "shared content", context=admin_ctx)

        # Grant alice reader on the file
        _grant_permission(nx, "alice", "direct_viewer", "/readme.txt")

        # Alice reads
        result = nx.read("/readme.txt", context=alice_ctx)
        if isinstance(result, bytes):
            result = result.decode("utf-8")
        assert result == "shared content"

    def test_delete_denied_without_grant(self, sql_nexus, alice_ctx, admin_ctx):
        """Alice cannot delete a file she doesn't own."""
        nx = sql_nexus

        nx.write("/admin/important.txt", "do not delete", context=admin_ctx)

        with pytest.raises((PermissionError, Exception)):
            nx.delete("/admin/important.txt", context=alice_ctx)

        # File should still exist
        assert nx.exists("/admin/important.txt", context=admin_ctx)

    def test_two_users_isolated(self, sql_nexus, alice_ctx, bob_ctx, admin_ctx):
        """Alice and bob have separate permissions — isolated from each other."""
        nx = sql_nexus

        # Admin creates files for both users
        nx.write("/alice_space/data.txt", "alice data", context=admin_ctx)
        nx.write("/bob_space/data.txt", "bob data", context=admin_ctx)

        # Grant read to respective users
        _grant_permission(nx, "alice", "direct_viewer", "/alice_space/data.txt")
        _grant_permission(nx, "bob", "direct_viewer", "/bob_space/data.txt")

        # Alice can read her file
        alice_result = nx.read("/alice_space/data.txt", context=alice_ctx)
        if isinstance(alice_result, bytes):
            alice_result = alice_result.decode("utf-8")
        assert alice_result == "alice data"

        # Bob can read his file
        bob_result = nx.read("/bob_space/data.txt", context=bob_ctx)
        if isinstance(bob_result, bytes):
            bob_result = bob_result.decode("utf-8")
        assert bob_result == "bob data"

        # Alice cannot read bob's file
        with pytest.raises((PermissionError, FileNotFoundError, Exception)):
            nx.read("/bob_space/data.txt", context=alice_ctx)

        # Bob cannot read alice's file
        with pytest.raises((PermissionError, FileNotFoundError, Exception)):
            nx.read("/alice_space/data.txt", context=bob_ctx)


# ---------------------------------------------------------------------------
# Test: SqlMetadataStore internals — no RecordStoreSyncer
# ---------------------------------------------------------------------------


class TestNoRecordStoreSyncer:
    """Verify that use_sql_metadata=True does NOT use RecordStoreSyncer."""

    def test_write_observer_is_none(self, sql_nexus):
        """write_observer should be None when use_sql_metadata=True."""
        nx = sql_nexus
        assert nx._write_observer is None, (
            "write_observer should be None when use_sql_metadata=True — "
            "SqlMetadataStore handles recording directly"
        )

    def test_metadata_store_is_sql(self, sql_nexus):
        """metadata store should be SqlMetadataStore, not RaftMetadataStore."""
        from nexus.storage.sql_metadata_store import SqlMetadataStore

        nx = sql_nexus
        assert isinstance(nx.metadata, SqlMetadataStore), (
            f"Expected SqlMetadataStore, got {type(nx.metadata).__name__}"
        )

    def test_operation_log_recorded(self, sql_nexus, admin_ctx):
        """Operations logged directly by SqlMetadataStore, not by observer."""
        from sqlalchemy import text

        nx = sql_nexus
        nx.write("/logged/file.txt", "content", context=admin_ctx)

        store = nx.metadata
        if hasattr(store, "_session_factory"):
            with store._session_factory() as session:
                rows = session.execute(
                    text("SELECT operation_type, path FROM operation_log WHERE path = :path"),
                    {"path": "/logged/file.txt"},
                ).fetchall()
                assert len(rows) >= 1, "Expected at least 1 operation log entry"
                assert rows[0][0] == "write"


# ---------------------------------------------------------------------------
# Test: Reconciler with real SQL + redb stores
# ---------------------------------------------------------------------------


class TestReconcilerIntegration:
    """Test reconciler with real SqlMetadataStore + RaftMetadataStore."""

    def test_reconcile_no_drift(self, sql_nexus, admin_ctx):
        """Reconciler reports no drift when stores are in sync."""
        from nexus.storage.reconciler import Reconciler

        nx = sql_nexus

        # Write some files
        nx.write("/sync/a.txt", "aaa", context=admin_ctx)
        nx.write("/sync/b.txt", "bbb", context=admin_ctx)

        # Get both stores
        sql_store = nx.metadata
        raft_store = getattr(sql_store, "_raft_store", None)

        if raft_store is None:
            pytest.skip("No raft_store configured")

        reconciler = Reconciler(sql_store, raft_store)
        stats = reconciler.reconcile_once()

        assert stats.errors == 0, f"Reconciler errors: {stats.details}"

    def test_reconcile_detects_stale_cache(self, sql_nexus, admin_ctx):
        """Reconciler detects and removes stale entries in redb cache."""
        from nexus.core._metadata_generated import FileMetadata
        from nexus.storage.reconciler import Reconciler

        nx = sql_nexus

        # Write a file via normal path (goes to both SQL and redb)
        nx.write("/stale/test.txt", "content", context=admin_ctx)

        sql_store = nx.metadata
        raft_store = getattr(sql_store, "_raft_store", None)
        if raft_store is None:
            pytest.skip("No raft_store configured")

        # Manually inject a stale entry directly into redb cache
        stale_meta = FileMetadata(
            path="/stale/orphan.txt",
            backend_name="local",
            physical_path="/stale/orphan.txt",
            size=42,
            etag="stale_hash",
            mime_type="text/plain",
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
            version=1,
        )
        raft_store.put(stale_meta)

        # Reconciler should detect and remove the stale entry
        reconciler = Reconciler(sql_store, raft_store)
        stats = reconciler.reconcile_once()

        assert stats.stale_cache_entries >= 1, (
            f"Expected stale entries, got {stats.stale_cache_entries}"
        )
        assert stats.repairs_applied >= 1, f"Expected repairs, got {stats.repairs_applied}"
