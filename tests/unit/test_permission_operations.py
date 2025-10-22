"""Tests for chmod/chown/chgrp permission enforcement (issue #112)."""

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext


class TestChmodPermissionEnforcement:
    """Tests for chmod permission checks."""

    def test_owner_can_chmod(self, tmp_path):
        """Test that file owner can chmod."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        # Create file owned by alice
        nx.write("/workspace/file.txt", b"test")
        meta = nx.metadata.get("/workspace/file.txt")
        meta.owner = "alice"
        meta.group = "users"
        meta.mode = 0o644
        nx.metadata.put(meta)

        # Alice can chmod
        alice_ctx = OperationContext(user="alice", groups=["users"])
        nx.chmod("/workspace/file.txt", 0o755, context=alice_ctx)

        # Verify mode changed
        meta = nx.metadata.get("/workspace/file.txt")
        assert meta.mode == 0o755

        nx.close()

    def test_non_owner_cannot_chmod(self, tmp_path):
        """Test that non-owner cannot chmod."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        # Create file owned by alice
        nx.write("/workspace/file.txt", b"test")
        meta = nx.metadata.get("/workspace/file.txt")
        meta.owner = "alice"
        meta.group = "users"
        meta.mode = 0o644
        nx.metadata.put(meta)

        # Bob cannot chmod
        bob_ctx = OperationContext(user="bob", groups=["users"])
        with pytest.raises(PermissionError, match="Only the owner.*can change permissions"):
            nx.chmod("/workspace/file.txt", 0o755, context=bob_ctx)

        nx.close()

    def test_admin_can_chmod_any_file(self, tmp_path):
        """Test that admin can chmod any file."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        # Create file owned by alice
        nx.write("/workspace/file.txt", b"test")
        meta = nx.metadata.get("/workspace/file.txt")
        meta.owner = "alice"
        meta.group = "users"
        meta.mode = 0o644
        nx.metadata.put(meta)

        # Admin can chmod
        admin_ctx = OperationContext(user="admin", groups=["admins"], is_admin=True)
        nx.chmod("/workspace/file.txt", 0o755, context=admin_ctx)

        # Verify mode changed
        meta = nx.metadata.get("/workspace/file.txt")
        assert meta.mode == 0o755

        nx.close()

    def test_system_can_chmod_any_file(self, tmp_path):
        """Test that system context can chmod any file."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        # Create file owned by alice
        nx.write("/workspace/file.txt", b"test")
        meta = nx.metadata.get("/workspace/file.txt")
        meta.owner = "alice"
        meta.group = "users"
        meta.mode = 0o644
        nx.metadata.put(meta)

        # System can chmod
        system_ctx = OperationContext(user="system", groups=[], is_system=True)
        nx.chmod("/workspace/file.txt", 0o755, context=system_ctx)

        # Verify mode changed
        meta = nx.metadata.get("/workspace/file.txt")
        assert meta.mode == 0o755

        nx.close()

    def test_chmod_with_string_mode(self, tmp_path):
        """Test chmod with string mode."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        # Create file
        nx.write("/workspace/file.txt", b"test")
        meta = nx.metadata.get("/workspace/file.txt")
        meta.owner = "alice"
        meta.mode = 0o644
        nx.metadata.put(meta)

        # chmod with octal string
        alice_ctx = OperationContext(user="alice", groups=[])
        nx.chmod("/workspace/file.txt", "755", context=alice_ctx)
        meta = nx.metadata.get("/workspace/file.txt")
        assert meta.mode == 0o755

        # chmod with symbolic string
        nx.chmod("/workspace/file.txt", "rw-r--r--", context=alice_ctx)
        meta = nx.metadata.get("/workspace/file.txt")
        assert meta.mode == 0o644

        nx.close()

    def test_chmod_nonexistent_file(self, tmp_path):
        """Test chmod on non-existent file."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        alice_ctx = OperationContext(user="alice", groups=[])
        with pytest.raises((PermissionError, FileNotFoundError)):
            nx.chmod("/workspace/nonexistent.txt", 0o755, context=alice_ctx)

        nx.close()


class TestChownPermissionEnforcement:
    """Tests for chown permission checks."""

    def test_owner_can_chown(self, tmp_path):
        """Test that file owner can chown."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        # Create file owned by alice
        nx.write("/workspace/file.txt", b"test")
        meta = nx.metadata.get("/workspace/file.txt")
        meta.owner = "alice"
        meta.group = "users"
        nx.metadata.put(meta)

        # Alice can chown
        alice_ctx = OperationContext(user="alice", groups=["users"])
        nx.chown("/workspace/file.txt", "bob", context=alice_ctx)

        # Verify owner changed
        meta = nx.metadata.get("/workspace/file.txt")
        assert meta.owner == "bob"

        nx.close()

    def test_non_owner_cannot_chown(self, tmp_path):
        """Test that non-owner cannot chown."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        # Create file owned by alice
        nx.write("/workspace/file.txt", b"test")
        meta = nx.metadata.get("/workspace/file.txt")
        meta.owner = "alice"
        meta.group = "users"
        nx.metadata.put(meta)

        # Bob cannot chown
        bob_ctx = OperationContext(user="bob", groups=["users"])
        with pytest.raises(PermissionError, match="Only the owner.*can change ownership"):
            nx.chown("/workspace/file.txt", "charlie", context=bob_ctx)

        nx.close()

    def test_admin_can_chown_any_file(self, tmp_path):
        """Test that admin can chown any file."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        # Create file owned by alice
        nx.write("/workspace/file.txt", b"test")
        meta = nx.metadata.get("/workspace/file.txt")
        meta.owner = "alice"
        nx.metadata.put(meta)

        # Admin can chown
        admin_ctx = OperationContext(user="admin", groups=[], is_admin=True)
        nx.chown("/workspace/file.txt", "bob", context=admin_ctx)

        # Verify owner changed
        meta = nx.metadata.get("/workspace/file.txt")
        assert meta.owner == "bob"

        nx.close()

    def test_chown_nonexistent_file(self, tmp_path):
        """Test chown on non-existent file."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        alice_ctx = OperationContext(user="alice", groups=[])
        with pytest.raises((PermissionError, FileNotFoundError)):
            nx.chown("/workspace/nonexistent.txt", "bob", context=alice_ctx)

        nx.close()


class TestChgrpPermissionEnforcement:
    """Tests for chgrp permission checks."""

    def test_owner_can_chgrp(self, tmp_path):
        """Test that file owner can chgrp."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        # Create file owned by alice
        nx.write("/workspace/file.txt", b"test")
        meta = nx.metadata.get("/workspace/file.txt")
        meta.owner = "alice"
        meta.group = "users"
        nx.metadata.put(meta)

        # Alice can chgrp
        alice_ctx = OperationContext(user="alice", groups=["users"])
        nx.chgrp("/workspace/file.txt", "developers", context=alice_ctx)

        # Verify group changed
        meta = nx.metadata.get("/workspace/file.txt")
        assert meta.group == "developers"

        nx.close()

    def test_non_owner_cannot_chgrp(self, tmp_path):
        """Test that non-owner cannot chgrp."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        # Create file owned by alice
        nx.write("/workspace/file.txt", b"test")
        meta = nx.metadata.get("/workspace/file.txt")
        meta.owner = "alice"
        meta.group = "users"
        nx.metadata.put(meta)

        # Bob cannot chgrp
        bob_ctx = OperationContext(user="bob", groups=["users"])
        with pytest.raises(PermissionError, match="Only the owner.*can change group"):
            nx.chgrp("/workspace/file.txt", "developers", context=bob_ctx)

        nx.close()

    def test_admin_can_chgrp_any_file(self, tmp_path):
        """Test that admin can chgrp any file."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        # Create file owned by alice
        nx.write("/workspace/file.txt", b"test")
        meta = nx.metadata.get("/workspace/file.txt")
        meta.owner = "alice"
        meta.group = "users"
        nx.metadata.put(meta)

        # Admin can chgrp
        admin_ctx = OperationContext(user="admin", groups=[], is_admin=True)
        nx.chgrp("/workspace/file.txt", "developers", context=admin_ctx)

        # Verify group changed
        meta = nx.metadata.get("/workspace/file.txt")
        assert meta.group == "developers"

        nx.close()

    def test_chgrp_nonexistent_file(self, tmp_path):
        """Test chgrp on non-existent file."""
        nx = NexusFS(
            backend=LocalBackend(tmp_path), db_path=tmp_path / "metadata.db", agent_id="alice"
        )

        alice_ctx = OperationContext(user="alice", groups=[])
        with pytest.raises((PermissionError, FileNotFoundError)):
            nx.chgrp("/workspace/nonexistent.txt", "developers", context=alice_ctx)

        nx.close()
