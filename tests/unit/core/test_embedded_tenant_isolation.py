"""Tests for tenant isolation in Embedded mode."""

import tempfile

import pytest

from nexus.core.embedded import Embedded
from nexus.core.router import AccessDeniedError


def test_tenant_isolation_read():
    """Test that tenants cannot read other tenants' files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Tenant ACME writes a file
        nx_acme = Embedded(data_dir=tmpdir, tenant_id="acme", is_admin=False)
        nx_acme.write("/workspace/acme/agent1/secret.txt", b"ACME secret data")
        nx_acme.close()

        # Tenant ACME can read their own file
        nx_acme2 = Embedded(data_dir=tmpdir, tenant_id="acme", is_admin=False)
        content = nx_acme2.read("/workspace/acme/agent1/secret.txt")
        assert content == b"ACME secret data"
        nx_acme2.close()

        # Tenant TechInc cannot read ACME's file
        nx_tech = Embedded(data_dir=tmpdir, tenant_id="techinc", is_admin=False)
        with pytest.raises(AccessDeniedError) as exc_info:
            nx_tech.read("/workspace/acme/agent1/secret.txt")
        assert "cannot access" in str(exc_info.value)
        nx_tech.close()


def test_tenant_isolation_write():
    """Test that tenants cannot write to other tenants' paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Tenant TechInc tries to write to ACME's workspace
        nx_tech = Embedded(data_dir=tmpdir, tenant_id="techinc", is_admin=False)
        with pytest.raises(AccessDeniedError) as exc_info:
            nx_tech.write("/workspace/acme/agent1/malicious.txt", b"hacked!")
        assert "cannot access" in str(exc_info.value)
        nx_tech.close()


def test_tenant_isolation_delete():
    """Test that tenants cannot delete other tenants' files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Tenant ACME creates a file
        nx_acme = Embedded(data_dir=tmpdir, tenant_id="acme", is_admin=False)
        nx_acme.write("/workspace/acme/agent1/important.txt", b"important data")
        nx_acme.close()

        # Tenant TechInc cannot delete ACME's file
        nx_tech = Embedded(data_dir=tmpdir, tenant_id="techinc", is_admin=False)
        with pytest.raises(AccessDeniedError) as exc_info:
            nx_tech.delete("/workspace/acme/agent1/important.txt")
        assert "cannot access" in str(exc_info.value)
        nx_tech.close()


def test_admin_can_access_all_tenants():
    """Test that admin can access any tenant's resources."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Tenant ACME writes a file
        nx_acme = Embedded(data_dir=tmpdir, tenant_id="acme", is_admin=False)
        nx_acme.write("/workspace/acme/agent1/data.txt", b"ACME data")
        nx_acme.close()

        # Admin (even with different tenant_id) can read it
        nx_admin = Embedded(data_dir=tmpdir, tenant_id="other", is_admin=True)
        content = nx_admin.read("/workspace/acme/agent1/data.txt")
        assert content == b"ACME data"

        # Admin can write to any tenant's path
        nx_admin.write("/workspace/acme/agent1/admin-note.txt", b"Admin was here")

        # Admin can delete any tenant's files
        nx_admin.delete("/workspace/acme/agent1/data.txt")
        nx_admin.close()


def test_readonly_namespace_enforcement():
    """Test that read-only namespaces reject writes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Try to write to /archives (read-only namespace)
        nx = Embedded(data_dir=tmpdir, tenant_id="acme", is_admin=False)

        with pytest.raises(AccessDeniedError) as exc_info:
            nx.write("/archives/acme/backup.tar", b"backup data")
        assert "read-only" in str(exc_info.value)
        nx.close()


def test_admin_only_namespace_enforcement():
    """Test that admin-only namespaces reject non-admin access."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Non-admin cannot access /system
        nx = Embedded(data_dir=tmpdir, tenant_id="acme", is_admin=False)

        with pytest.raises(AccessDeniedError) as exc_info:
            nx.write("/system/config.json", b'{"setting": "value"}')
        assert "requires admin" in str(exc_info.value)
        nx.close()

        # Admin can access /system
        nx_admin = Embedded(data_dir=tmpdir, tenant_id="acme", is_admin=True)
        # Admin cannot write because /system is read-only
        with pytest.raises(AccessDeniedError) as exc_info:
            nx_admin.write("/system/config.json", b'{"setting": "value"}')
        assert "read-only" in str(exc_info.value)
        nx_admin.close()


def test_shared_namespace_tenant_isolation():
    """Test that shared namespace enforces tenant isolation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # ACME tenant writes to shared
        nx_acme = Embedded(data_dir=tmpdir, tenant_id="acme", is_admin=False)
        nx_acme.write("/shared/acme/models/classifier.pkl", b"ACME model")
        nx_acme.close()

        # TechInc cannot access ACME's shared data
        nx_tech = Embedded(data_dir=tmpdir, tenant_id="techinc", is_admin=False)
        with pytest.raises(AccessDeniedError) as exc_info:
            nx_tech.read("/shared/acme/models/classifier.pkl")
        assert "cannot access" in str(exc_info.value)
        nx_tech.close()


def test_no_tenant_id_bypasses_isolation():
    """Test that operations without tenant_id are not restricted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Without tenant_id, can write anywhere (for backwards compatibility)
        nx = Embedded(data_dir=tmpdir, tenant_id=None, is_admin=False)
        nx.write("/workspace/acme/agent1/file.txt", b"data")
        nx.write("/workspace/techinc/agent1/file.txt", b"data")

        # Can read from any tenant
        content1 = nx.read("/workspace/acme/agent1/file.txt")
        content2 = nx.read("/workspace/techinc/agent1/file.txt")

        assert content1 == b"data"
        assert content2 == b"data"
        nx.close()


def test_directory_operations_enforce_isolation():
    """Test that directory operations respect tenant isolation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Tenant ACME creates a directory
        nx_acme = Embedded(data_dir=tmpdir, tenant_id="acme", is_admin=False)
        nx_acme.mkdir("/workspace/acme/agent1/data", parents=True, exist_ok=True)
        nx_acme.close()

        # Tenant TechInc cannot remove ACME's directory
        nx_tech = Embedded(data_dir=tmpdir, tenant_id="techinc", is_admin=False)
        with pytest.raises(AccessDeniedError) as exc_info:
            nx_tech.rmdir("/workspace/acme/agent1/data", recursive=True)
        assert "cannot access" in str(exc_info.value)
        nx_tech.close()


# === Agent Isolation Tests ===


def test_agent_isolation_read():
    """Test that agents cannot read other agents' workspace files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Agent1 writes to their workspace
        nx_agent1 = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent1")
        nx_agent1.write("/workspace/acme/agent1/private.txt", b"agent1 secret")
        nx_agent1.close()

        # Agent1 can read their own file
        nx_agent1_again = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent1")
        content = nx_agent1_again.read("/workspace/acme/agent1/private.txt")
        assert content == b"agent1 secret"
        nx_agent1_again.close()

        # Agent2 cannot read Agent1's file
        nx_agent2 = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent2")
        with pytest.raises(AccessDeniedError) as exc_info:
            nx_agent2.read("/workspace/acme/agent1/private.txt")
        assert "agent 'agent2' cannot access agent 'agent1' workspace" in str(exc_info.value)
        nx_agent2.close()


def test_agent_isolation_write():
    """Test that agents cannot write to other agents' workspace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Agent2 tries to write to Agent1's workspace
        nx_agent2 = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent2")
        with pytest.raises(AccessDeniedError) as exc_info:
            nx_agent2.write("/workspace/acme/agent1/hacked.txt", b"malicious")
        assert "agent 'agent2' cannot access agent 'agent1' workspace" in str(exc_info.value)
        nx_agent2.close()


def test_agent_isolation_delete():
    """Test that agents cannot delete other agents' workspace files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Agent1 creates a file
        nx_agent1 = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent1")
        nx_agent1.write("/workspace/acme/agent1/important.txt", b"critical data")
        nx_agent1.close()

        # Agent2 cannot delete Agent1's file
        nx_agent2 = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent2")
        with pytest.raises(AccessDeniedError) as exc_info:
            nx_agent2.delete("/workspace/acme/agent1/important.txt")
        assert "agent 'agent2' cannot access agent 'agent1' workspace" in str(exc_info.value)
        nx_agent2.close()


def test_agent_isolation_mkdir():
    """Test that agents cannot create directories in other agents' workspace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Agent2 tries to create directory in Agent1's workspace
        nx_agent2 = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent2")
        with pytest.raises(AccessDeniedError) as exc_info:
            nx_agent2.mkdir("/workspace/acme/agent1/newdir", parents=True)
        assert "agent 'agent2' cannot access agent 'agent1' workspace" in str(exc_info.value)
        nx_agent2.close()


def test_agent_isolation_rmdir():
    """Test that agents cannot remove other agents' directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Agent1 creates a directory
        nx_agent1 = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent1")
        nx_agent1.mkdir("/workspace/acme/agent1/data", parents=True)
        nx_agent1.close()

        # Agent2 cannot remove Agent1's directory
        nx_agent2 = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent2")
        with pytest.raises(AccessDeniedError) as exc_info:
            nx_agent2.rmdir("/workspace/acme/agent1/data", recursive=True)
        assert "agent 'agent2' cannot access agent 'agent1' workspace" in str(exc_info.value)
        nx_agent2.close()


def test_agents_can_access_shared_namespace():
    """Test that agents in same tenant can access shared namespace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Agent1 writes to shared
        nx_agent1 = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent1")
        nx_agent1.write("/shared/acme/models/v1.pkl", b"model data")
        nx_agent1.close()

        # Agent2 can read from shared
        nx_agent2 = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent2")
        content = nx_agent2.read("/shared/acme/models/v1.pkl")
        assert content == b"model data"

        # Agent2 can write to shared
        nx_agent2.write("/shared/acme/models/v2.pkl", b"new model")
        nx_agent2.close()

        # Agent1 can read Agent2's shared file
        nx_agent1_again = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent1")
        content2 = nx_agent1_again.read("/shared/acme/models/v2.pkl")
        assert content2 == b"new model"
        nx_agent1_again.close()


def test_admin_can_access_all_agents():
    """Test that admin can access any agent's workspace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Agent1 writes to their workspace
        nx_agent1 = Embedded(data_dir=tmpdir, tenant_id="acme", agent_id="agent1")
        nx_agent1.write("/workspace/acme/agent1/data.txt", b"agent1 data")
        nx_agent1.close()

        # Admin can read any agent's workspace
        nx_admin = Embedded(data_dir=tmpdir, tenant_id="acme", is_admin=True)
        content = nx_admin.read("/workspace/acme/agent1/data.txt")
        assert content == b"agent1 data"

        # Admin can write to any agent's workspace
        nx_admin.write("/workspace/acme/agent1/admin-edit.txt", b"admin was here")
        nx_admin.close()


def test_no_agent_id_allows_access_to_any_agent():
    """Test that without agent_id, can access any agent's workspace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Without agent_id, can write to any agent's workspace
        nx = Embedded(data_dir=tmpdir, tenant_id="acme")
        nx.write("/workspace/acme/agent1/file1.txt", b"data1")
        nx.write("/workspace/acme/agent2/file2.txt", b"data2")

        # Can read from any agent
        content1 = nx.read("/workspace/acme/agent1/file1.txt")
        content2 = nx.read("/workspace/acme/agent2/file2.txt")

        assert content1 == b"data1"
        assert content2 == b"data2"
        nx.close()
