"""Tests for namespace operations in Embedded mode using only user-facing APIs."""

import tempfile

import pytest

from nexus.core.embedded import Embedded
from nexus.core.router import AccessDeniedError, NamespaceConfig


def test_workspace_namespace_operations():
    """Test basic operations in workspace namespace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent1")

        # Write to workspace
        nx.write("/workspace/acme/agent1/code.py", b"print('hello')")

        # Read back
        content = nx.read("/workspace/acme/agent1/code.py")
        assert content == b"print('hello')"

        # Check existence
        assert nx.exists("/workspace/acme/agent1/code.py")

        # List files
        files = nx.list("/workspace/acme/agent1")
        assert "/workspace/acme/agent1/code.py" in files

        # Delete
        nx.delete("/workspace/acme/agent1/code.py")
        assert not nx.exists("/workspace/acme/agent1/code.py")

        nx.close()


def test_shared_namespace_operations():
    """Test basic operations in shared namespace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = Embedded(data_dir=tmpdir, tenant_id="acme")

        # Write to shared
        nx.write("/shared/acme/models/model.pkl", b"model data")

        # Read back
        content = nx.read("/shared/acme/models/model.pkl")
        assert content == b"model data"

        # Check existence
        assert nx.exists("/shared/acme/models/model.pkl")

        # List files
        files = nx.list("/shared/acme/models")
        assert "/shared/acme/models/model.pkl" in files

        nx.close()


def test_external_namespace_operations():
    """Test basic operations in external namespace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = Embedded(data_dir=tmpdir)

        # External namespace doesn't require tenant isolation
        nx.write("/external/s3/bucket/file.txt", b"external data")

        # Read back
        content = nx.read("/external/s3/bucket/file.txt")
        assert content == b"external data"

        # Check existence
        assert nx.exists("/external/s3/bucket/file.txt")

        nx.close()


def test_archives_namespace_readonly():
    """Test that archives namespace is read-only."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = Embedded(data_dir=tmpdir, tenant_id="acme")

        # Archives is read-only - cannot write
        with pytest.raises(AccessDeniedError) as exc_info:
            nx.write("/archives/acme/2024/backup.tar", b"backup")
        assert "read-only" in str(exc_info.value)

        nx.close()


def test_system_namespace_admin_only():
    """Test that system namespace requires admin privileges."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Non-admin cannot access system namespace
        nx = Embedded(data_dir=tmpdir, is_admin=False)

        with pytest.raises(AccessDeniedError) as exc_info:
            nx.read("/system/config.json")
        assert "requires admin" in str(exc_info.value)

        nx.close()

        # Admin can access (but cannot write since read-only)
        nx_admin = Embedded(data_dir=tmpdir, is_admin=True)

        with pytest.raises(AccessDeniedError) as exc_info:
            nx_admin.write("/system/config.json", b"config")
        assert "read-only" in str(exc_info.value)

        nx_admin.close()


def test_multi_namespace_operations_single_tenant():
    """Test operations across multiple namespaces for a single tenant."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Agent1 in ACME tenant
        nx_agent1 = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent1")

        # Write to workspace (agent-specific)
        nx_agent1.write("/workspace/acme/agent1/task.json", b'{"status": "running"}')

        # Write to shared (tenant-wide)
        nx_agent1.write("/shared/acme/config.json", b'{"version": "1.0"}')

        # Write to external (no isolation)
        nx_agent1.write("/external/cache/data.json", b'{"cached": true}')

        # Verify all files exist
        assert nx_agent1.exists("/workspace/acme/agent1/task.json")
        assert nx_agent1.exists("/shared/acme/config.json")
        assert nx_agent1.exists("/external/cache/data.json")

        nx_agent1.close()

        # Agent2 in same tenant
        nx_agent2 = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent2")

        # Cannot access Agent1's workspace
        with pytest.raises(AccessDeniedError):
            nx_agent2.read("/workspace/acme/agent1/task.json")

        # Can access shared namespace
        content = nx_agent2.read("/shared/acme/config.json")
        assert content == b'{"version": "1.0"}'

        # Can access external namespace
        content = nx_agent2.read("/external/cache/data.json")
        assert content == b'{"cached": true}'

        nx_agent2.close()


def test_multi_namespace_operations_multi_tenant():
    """Test operations across multiple namespaces with multiple tenants."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # ACME tenant
        nx_acme = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent1")
        nx_acme.write("/workspace/acme/agent1/data.txt", b"ACME workspace")
        nx_acme.write("/shared/acme/team-data.txt", b"ACME shared")
        nx_acme.close()

        # TechInc tenant
        nx_tech = Embedded(data_dir=tmpdir, tenant_id="techinc", agent_id="agent1")

        # Cannot access ACME's workspace
        with pytest.raises(AccessDeniedError):
            nx_tech.read("/workspace/acme/agent1/data.txt")

        # Cannot access ACME's shared
        with pytest.raises(AccessDeniedError):
            nx_tech.read("/shared/acme/team-data.txt")

        # Can write to own workspace
        nx_tech.write("/workspace/techinc/agent1/data.txt", b"TechInc workspace")
        assert nx_tech.exists("/workspace/techinc/agent1/data.txt")

        # Can write to own shared
        nx_tech.write("/shared/techinc/team-data.txt", b"TechInc shared")
        assert nx_tech.exists("/shared/techinc/team-data.txt")

        nx_tech.close()


def test_custom_namespace_configuration():
    """Test registering and using custom namespaces."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Define custom namespace
        custom_ns = NamespaceConfig(
            name="analytics",
            readonly=False,
            admin_only=False,
            requires_tenant=True,
        )

        # Create Embedded instance with custom namespace
        nx = Embedded(data_dir=tmpdir, tenant_id="acme", custom_namespaces=[custom_ns])

        # Use custom namespace
        nx.write("/analytics/acme/metrics/daily.json", b'{"views": 1000}')

        # Read back
        content = nx.read("/analytics/acme/metrics/daily.json")
        assert content == b'{"views": 1000}'

        # Verify tenant isolation works for custom namespace
        nx.close()

        nx_other = Embedded(data_dir=tmpdir, tenant_id="other", custom_namespaces=[custom_ns])

        with pytest.raises(AccessDeniedError):
            nx_other.read("/analytics/acme/metrics/daily.json")

        nx_other.close()


def test_custom_namespace_readonly():
    """Test custom read-only namespace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Define read-only custom namespace
        readonly_ns = NamespaceConfig(
            name="static", readonly=True, admin_only=False, requires_tenant=False
        )

        nx = Embedded(data_dir=tmpdir, custom_namespaces=[readonly_ns])

        # Cannot write to read-only namespace
        with pytest.raises(AccessDeniedError) as exc_info:
            nx.write("/static/assets/logo.png", b"image data")
        assert "read-only" in str(exc_info.value)

        nx.close()


def test_custom_namespace_admin_only():
    """Test custom admin-only namespace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Define admin-only custom namespace
        admin_ns = NamespaceConfig(
            name="audit", readonly=False, admin_only=True, requires_tenant=False
        )

        # Non-admin cannot access
        nx = Embedded(data_dir=tmpdir, is_admin=False, custom_namespaces=[admin_ns])

        with pytest.raises(AccessDeniedError) as exc_info:
            nx.write("/audit/logs/access.log", b"log entry")
        assert "requires admin" in str(exc_info.value)

        nx.close()

        # Admin can access
        nx_admin = Embedded(data_dir=tmpdir, is_admin=True, custom_namespaces=[admin_ns])

        nx_admin.write("/audit/logs/access.log", b"log entry")
        content = nx_admin.read("/audit/logs/access.log")
        assert content == b"log entry"

        nx_admin.close()


def test_directory_operations_across_namespaces():
    """Test directory operations work across different namespaces."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent1")

        # Create directories in workspace
        nx.mkdir("/workspace/acme/agent1/data", parents=True)
        assert nx.is_directory("/workspace/acme/agent1/data")

        # Create directories in shared
        nx.mkdir("/shared/acme/models", parents=True)
        assert nx.is_directory("/shared/acme/models")

        # Create directories in external
        nx.mkdir("/external/cache", parents=True)
        assert nx.is_directory("/external/cache")

        # Write files
        nx.write("/workspace/acme/agent1/data/file1.txt", b"data1")
        nx.write("/shared/acme/models/model.pkl", b"model")
        nx.write("/external/cache/temp.json", b"temp")

        # Remove directories
        nx.rmdir("/workspace/acme/agent1/data", recursive=True)
        assert not nx.is_directory("/workspace/acme/agent1/data")

        nx.close()


def test_list_operations_across_namespaces():
    """Test list operations work correctly across namespaces."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent1")

        # Write files to different namespaces
        nx.write("/workspace/acme/agent1/file1.txt", b"data1")
        nx.write("/workspace/acme/agent1/file2.txt", b"data2")
        nx.write("/shared/acme/file3.txt", b"data3")
        nx.write("/external/file4.txt", b"data4")

        # List workspace files
        workspace_files = nx.list("/workspace/acme/agent1")
        assert len(workspace_files) == 2
        assert "/workspace/acme/agent1/file1.txt" in workspace_files
        assert "/workspace/acme/agent1/file2.txt" in workspace_files

        # List shared files
        shared_files = nx.list("/shared/acme")
        assert "/shared/acme/file3.txt" in shared_files

        # List external files
        external_files = nx.list("/external")
        assert "/external/file4.txt" in external_files

        # List all files
        all_files = nx.list()
        assert len(all_files) >= 4

        nx.close()


def test_admin_can_access_all_namespaces():
    """Test that admin can access all tenant-isolated namespaces."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Regular user writes files
        nx_acme = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent1")
        nx_acme.write("/workspace/acme/agent1/secret.txt", b"secret")
        nx_acme.write("/shared/acme/data.txt", b"shared data")
        nx_acme.close()

        # Admin with different tenant_id can access all
        nx_admin = Embedded(data_dir=tmpdir, tenant_id="other", is_admin=True)

        # Can read ACME's workspace
        content = nx_admin.read("/workspace/acme/agent1/secret.txt")
        assert content == b"secret"

        # Can read ACME's shared
        content = nx_admin.read("/shared/acme/data.txt")
        assert content == b"shared data"

        # Can modify ACME's files
        nx_admin.write("/workspace/acme/agent1/admin.txt", b"admin was here")
        nx_admin.delete("/workspace/acme/agent1/secret.txt")

        nx_admin.close()
