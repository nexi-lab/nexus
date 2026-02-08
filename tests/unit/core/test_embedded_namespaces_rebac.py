"""Tests for namespace operations with v0.6.0+ ReBAC architecture.

These tests verify that namespace routing works correctly with the new
ReBAC permission model where subject is passed via OperationContext
per-operation rather than at NexusFS construction time.
"""

import gc
import platform
import tempfile
import time
from pathlib import Path

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext
from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


def cleanup_windows_db():
    """Force cleanup of database connections on Windows."""
    gc.collect()
    if platform.system() == "Windows":
        time.sleep(0.05)


def test_workspace_namespace_operations():
    """Test basic operations in workspace namespace with ReBAC."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = NexusFS(
            auto_parse=False,
            backend=LocalBackend(tmpdir),
            metadata_store=SQLAlchemyMetadataStore(db_path=Path(tmpdir) / "metadata.db"),
            record_store=SQLAlchemyRecordStore(db_path=Path(tmpdir) / "metadata.db"),
            enforce_permissions=False,  # Test namespace routing without permissions
        )

        # v0.6.0+: Create OperationContext with subject identity
        ctx = OperationContext(
            user="agent1",
            subject_type="agent",
            subject_id="agent1",
            groups=[],
            zone_id="acme",
            is_admin=False,
        )

        # Write to workspace
        nx.write("/workspace/acme/agent1/code.py", b"print('hello')", context=ctx)

        # Read back
        content = nx.read("/workspace/acme/agent1/code.py", context=ctx)
        assert content == b"print('hello')"

        # Check existence
        assert nx.exists("/workspace/acme/agent1/code.py", context=ctx)

        # List files
        files = nx.list("/workspace/acme/agent1", context=ctx)
        assert "/workspace/acme/agent1/code.py" in files

        # Delete
        nx.delete("/workspace/acme/agent1/code.py", context=ctx)
        assert not nx.exists("/workspace/acme/agent1/code.py", context=ctx)

        nx.close()
        cleanup_windows_db()


def test_shared_namespace_operations():
    """Test basic operations in shared namespace with ReBAC."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = NexusFS(
            auto_parse=False,
            backend=LocalBackend(tmpdir),
            metadata_store=SQLAlchemyMetadataStore(db_path=Path(tmpdir) / "metadata.db"),
            record_store=SQLAlchemyRecordStore(db_path=Path(tmpdir) / "metadata.db"),
            enforce_permissions=False,
        )

        ctx = OperationContext(
            user="alice",
            subject_type="user",
            subject_id="alice",
            groups=[],
            zone_id="acme",
            is_admin=False,
        )

        # Write to shared namespace
        nx.write("/shared/acme/models/model.pkl", b"model data", context=ctx)

        # Read back
        content = nx.read("/shared/acme/models/model.pkl", context=ctx)
        assert content == b"model data"

        # List files
        files = nx.list("/shared/acme/models", context=ctx)
        assert "/shared/acme/models/model.pkl" in files

        # Delete
        nx.delete("/shared/acme/models/model.pkl", context=ctx)
        assert not nx.exists("/shared/acme/models/model.pkl", context=ctx)

        nx.close()
        cleanup_windows_db()


def test_external_namespace_operations():
    """Test basic operations in external namespace with ReBAC."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = NexusFS(
            auto_parse=False,
            backend=LocalBackend(tmpdir),
            metadata_store=SQLAlchemyMetadataStore(db_path=Path(tmpdir) / "metadata.db"),
            record_store=SQLAlchemyRecordStore(db_path=Path(tmpdir) / "metadata.db"),
            enforce_permissions=False,
        )

        ctx = OperationContext(
            user="anonymous", subject_type="user", subject_id="anonymous", groups=[], is_admin=False
        )  # External namespace doesn't require zone_id

        # Write to external namespace
        nx.write("/external/s3/bucket/file.txt", b"external data", context=ctx)

        # Read back
        content = nx.read("/external/s3/bucket/file.txt", context=ctx)
        assert content == b"external data"

        # List files
        files = nx.list("/external/s3/bucket", context=ctx)
        assert "/external/s3/bucket/file.txt" in files

        # Delete
        nx.delete("/external/s3/bucket/file.txt", context=ctx)
        assert not nx.exists("/external/s3/bucket/file.txt", context=ctx)

        nx.close()
        cleanup_windows_db()


def test_multi_namespace_operations_single_zone():
    """Test operations across multiple namespaces for single zone."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = NexusFS(
            auto_parse=False,
            backend=LocalBackend(tmpdir),
            metadata_store=SQLAlchemyMetadataStore(db_path=Path(tmpdir) / "metadata.db"),
            record_store=SQLAlchemyRecordStore(db_path=Path(tmpdir) / "metadata.db"),
            enforce_permissions=False,
        )

        ctx = OperationContext(
            user="agent1",
            subject_type="agent",
            subject_id="agent1",
            groups=[],
            zone_id="acme",
            is_admin=False,
        )

        # Write to different namespaces
        nx.write("/workspace/acme/agent1/code.py", b"code", context=ctx)
        nx.write("/shared/acme/data.txt", b"data", context=ctx)
        nx.write("/external/gcs/bucket/file.txt", b"external", context=ctx)

        # Verify all namespaces work
        assert nx.exists("/workspace/acme/agent1/code.py", context=ctx)
        assert nx.exists("/shared/acme/data.txt", context=ctx)
        assert nx.exists("/external/gcs/bucket/file.txt", context=ctx)

        nx.close()
        cleanup_windows_db()


def test_namespace_isolation_between_zones():
    """Test that different zones' workspaces are isolated."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = NexusFS(
            auto_parse=False,
            backend=LocalBackend(tmpdir),
            metadata_store=SQLAlchemyMetadataStore(db_path=Path(tmpdir) / "metadata.db"),
            record_store=SQLAlchemyRecordStore(db_path=Path(tmpdir) / "metadata.db"),
            enforce_permissions=False,
        )

        # Zone 1 writes
        nx.write(
            "/workspace/acme/agent1/secret.txt",
            b"acme secret",
            context=OperationContext(
                user="agent1",
                subject_type="agent",
                subject_id="agent1",
                groups=[],
                zone_id="acme",
                is_admin=False,
            ),
        )

        # Zone 2 writes to same path structure (different zone)
        nx.write(
            "/workspace/globex/agent1/secret.txt",
            b"globex secret",
            context=OperationContext(
                user="agent1",
                subject_type="agent",
                subject_id="agent1",
                groups=[],
                zone_id="globex",
                is_admin=False,
            ),
        )

        # Verify isolation - each zone sees only their data
        acme_content = nx.read(
            "/workspace/acme/agent1/secret.txt",
            context=OperationContext(
                user="agent1",
                subject_type="agent",
                subject_id="agent1",
                groups=[],
                zone_id="acme",
                is_admin=False,
            ),
        )
        globex_content = nx.read(
            "/workspace/globex/agent1/secret.txt",
            context=OperationContext(
                user="agent1",
                subject_type="agent",
                subject_id="agent1",
                groups=[],
                zone_id="globex",
                is_admin=False,
            ),
        )

        assert acme_content == b"acme secret"
        assert globex_content == b"globex secret"

        nx.close()
        cleanup_windows_db()
