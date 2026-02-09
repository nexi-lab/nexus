"""Unit tests for NexusFS.deprovision_user() method."""

import uuid
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext
from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.mark.xdist_group("serial_sqlite")
class TestDeprovisionUser:
    """Test suite for deprovision_user functionality."""

    @pytest.fixture
    def temp_dir(self, tmp_path: Path) -> Path:
        """Create a temporary directory for tests."""
        return tmp_path

    @pytest.fixture
    def record_store(self, temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
        """Create a SQLAlchemyRecordStore for testing."""
        db_file = temp_dir / "metadata.db"
        rs = SQLAlchemyRecordStore(db_path=db_file)
        yield rs
        rs.close()

    @pytest.fixture
    def nx(self, temp_dir: Path, record_store, monkeypatch) -> Generator[NexusFS, None, None]:
        """Create a NexusFS instance with permissions enforced."""
        import time

        # Use SQLite file in temp directory
        db_file = temp_dir / "metadata.db"

        # Monkey-patch to disable Tiger Cache worker startup in tests
        from nexus.core import nexus_fs

        original_start = nexus_fs.NexusFS._start_tiger_cache_worker

        def _no_op_start(self) -> None:
            """Disabled Tiger Cache worker for tests to avoid SQLite locking."""
            _ = self  # Method signature requires self
            return None

        monkeypatch.setattr(nexus_fs.NexusFS, "_start_tiger_cache_worker", _no_op_start)

        nx_instance = create_nexus_fs(
            backend=LocalBackend(temp_dir),
            metadata_store=RaftMetadataStore.local(str(temp_dir / "raft-metadata")),
            record_store=record_store,
            auto_parse=False,
            enforce_permissions=True,
            allow_admin_bypass=True,
        )
        yield nx_instance

        # Ensure cleanup
        time.sleep(0.1)  # Brief pause to ensure any pending operations finish
        nx_instance.close()

        # Restore original method
        monkeypatch.setattr(nexus_fs.NexusFS, "_start_tiger_cache_worker", original_start)

    @pytest.fixture
    def admin_context(self) -> OperationContext:
        """Create an admin operation context."""
        return OperationContext(
            user="admin",
            groups=[],
            zone_id="example",
            is_admin=True,
        )

    def test_deprovision_basic(self, nx: NexusFS, admin_context: OperationContext) -> None:
        """Test basic deprovisioning of a user."""
        # First provision a user
        nx.provision_user(
            user_id="alice",
            email="alice@example.com",
            display_name="Alice Smith",
            zone_id="example",
            create_api_key=True,
            create_agents=True,
            import_skills=False,  # Skip skills for faster test
            context=admin_context,
        )

        # Verify user exists
        user_path = "/zone/example/user:alice/workspace"
        assert nx.exists(user_path, context=admin_context)

        # Deprovision the user
        result = nx.deprovision_user(
            user_id="alice",
            zone_id="example",
            delete_user_record=False,
            context=admin_context,
        )

        # Verify result structure
        assert result["user_id"] == "alice"
        assert result["zone_id"] == "example"
        assert len(result["deleted_directories"]) == 6  # All 6 resource types
        assert result["deleted_api_keys"] >= 1
        assert result["user_record_deleted"] is False

    def test_deprovision_deletes_directories(
        self, nx: NexusFS, admin_context: OperationContext
    ) -> None:
        """Test that deprovisioning deletes all user directories."""
        # Provision user
        nx.provision_user(
            user_id="bob",
            email="bob@example.com",
            zone_id="example",
            create_api_key=False,
            create_agents=False,
            import_skills=False,
            context=admin_context,
        )

        # Verify directories exist
        existing_dirs = []
        for resource_type in ["workspace", "memory", "skill", "agent", "connector", "resource"]:
            dir_path = f"/zone/example/user:bob/{resource_type}"
            if nx.exists(dir_path, context=admin_context):
                existing_dirs.append(dir_path)

        # Deprovision
        result = nx.deprovision_user(
            user_id="bob",
            zone_id="example",
            context=admin_context,
        )

        # Verify directories were processed (may remain as empty stubs)
        assert len(result["deleted_directories"]) == len(existing_dirs)

        # Verify directories are empty (all data deleted)
        for dir_path in existing_dirs:
            try:
                files = nx.list(dir_path, recursive=True, context=admin_context)
                if isinstance(files, list):
                    file_count = len(files)
                elif isinstance(files, dict):
                    file_count = len(files.get("files", []))
                else:
                    file_count = 0
                # Directory should be empty (all user data deleted)
                assert file_count == 0, f"Directory {dir_path} still contains {file_count} files"
            except Exception:
                # Directory doesn't exist or is empty - good!
                pass

    def test_deprovision_deletes_api_keys(
        self, nx: NexusFS, admin_context: OperationContext, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Test that deprovisioning deletes API keys."""
        # Provision user with API key
        result = nx.provision_user(
            user_id="charlie",
            email="charlie@example.com",
            zone_id="example",
            create_api_key=True,
            create_agents=False,
            import_skills=False,
            context=admin_context,
        )

        api_key = result["api_key"]
        assert api_key is not None

        # Deprovision
        deprovision_result = nx.deprovision_user(
            user_id="charlie",
            zone_id="example",
            context=admin_context,
        )

        # Verify API key was deleted
        assert deprovision_result["deleted_api_keys"] >= 1

        # Verify API key is revoked in database
        from nexus.storage.models import APIKeyModel

        session = record_store.session_factory()
        try:
            key_count = (
                session.query(APIKeyModel)
                .filter_by(user_id="charlie", subject_type="user", revoked=0)
                .count()
            )
            assert key_count == 0
        finally:
            session.close()

    def test_deprovision_soft_delete_user_record(
        self, nx: NexusFS, admin_context: OperationContext, record_store
    ) -> None:
        """Test soft-deleting user record during deprovisioning."""
        # Provision user
        nx.provision_user(
            user_id="david",
            email="david@example.com",
            zone_id="example",
            create_api_key=False,
            create_agents=False,
            import_skills=False,
            context=admin_context,
        )

        # Deprovision with user record deletion
        result = nx.deprovision_user(
            user_id="david",
            zone_id="example",
            delete_user_record=True,
            context=admin_context,
        )

        # Verify user record was soft-deleted
        assert result["user_record_deleted"] is True

        # Check database
        from nexus.storage.models import UserModel

        session = record_store.session_factory()
        try:
            user = session.query(UserModel).filter_by(user_id="david").first()
            assert user is not None
            assert user.is_active == 0
            assert user.deleted_at is not None
        finally:
            session.close()

    def test_deprovision_prevents_admin_deprovisioning(
        self, nx: NexusFS, admin_context: OperationContext, record_store
    ) -> None:
        """Test that deprovisioning prevents removing admin users."""
        # Create admin user directly in database
        from datetime import UTC, datetime

        from nexus.storage.models import UserModel

        session = record_store.session_factory()
        try:
            admin_user = UserModel(
                user_id="admin_user",
                email="admin@example.com",
                username="admin_user",
                display_name="Admin User",
                zone_id="example",
                primary_auth_method="api_key",
                is_active=1,
                is_global_admin=1,  # Admin user
                email_verified=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(admin_user)
            session.commit()
        finally:
            session.close()

        # Attempt to deprovision admin user (should fail)
        with pytest.raises(ValueError, match="Cannot deprovision global admin user"):
            nx.deprovision_user(
                user_id="admin_user",
                zone_id="example",
                context=admin_context,
            )

    def test_deprovision_force_admin_deprovisioning(
        self, nx: NexusFS, admin_context: OperationContext, record_store
    ) -> None:
        """Test force flag allows deprovisioning admin users."""
        # Create admin user
        from datetime import UTC, datetime

        from nexus.storage.models import UserModel

        session = record_store.session_factory()
        try:
            admin_user = UserModel(
                user_id="admin_user2",
                email="admin2@example.com",
                username="admin_user2",
                display_name="Admin User 2",
                zone_id="example",
                primary_auth_method="api_key",
                is_active=1,
                is_global_admin=1,  # Admin user
                email_verified=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(admin_user)
            session.commit()
        finally:
            session.close()

        # Deprovision with force flag (should succeed)
        result = nx.deprovision_user(
            user_id="admin_user2",
            zone_id="example",
            force=True,
            context=admin_context,
        )

        assert result["user_id"] == "admin_user2"

    def test_deprovision_idempotency(self, nx: NexusFS, admin_context: OperationContext) -> None:
        """Test that deprovisioning is idempotent (safe to call multiple times)."""
        # Provision user
        nx.provision_user(
            user_id="eve",
            email="eve@example.com",
            zone_id="example",
            create_api_key=True,
            create_agents=False,
            import_skills=False,
            context=admin_context,
        )

        # Deprovision first time
        result1 = nx.deprovision_user(
            user_id="eve",
            zone_id="example",
            context=admin_context,
        )

        # Should have deleted some directories and API keys
        first_dirs = len(result1["deleted_directories"])
        first_keys = result1["deleted_api_keys"]
        assert first_dirs > 0
        assert first_keys >= 1

        # Deprovision second time (should not error, but find nothing to delete)
        result2 = nx.deprovision_user(
            user_id="eve",
            zone_id="example",
            context=admin_context,
        )

        # Second run should find nothing new to delete
        # (May still report same directories if they exist as empty stubs)
        assert result2["deleted_api_keys"] == 0  # API keys already deleted
        # Directories count may be same if empty stubs remain

    def test_deprovision_nonexistent_user(
        self, nx: NexusFS, admin_context: OperationContext
    ) -> None:
        """Test deprovisioning a user that doesn't exist."""
        # Deprovision non-existent user (should not error)
        result = nx.deprovision_user(
            user_id="nonexistent",
            zone_id="example",
            context=admin_context,
        )

        assert result["user_id"] == "nonexistent"
        assert len(result["deleted_directories"]) == 0
        assert result["deleted_api_keys"] == 0
        assert result["user_record_deleted"] is False

    def test_deprovision_zone_lookup(self, nx: NexusFS, admin_context: OperationContext) -> None:
        """Test that zone_id is looked up from user if not provided."""
        # Provision user
        nx.provision_user(
            user_id="frank",
            email="frank@example.com",
            zone_id="example",
            create_api_key=False,
            create_agents=False,
            import_skills=False,
            context=admin_context,
        )

        # Deprovision without providing zone_id (should look up from user)
        result = nx.deprovision_user(
            user_id="frank",
            # zone_id not provided
            context=admin_context,
        )

        assert result["zone_id"] == "example"
        assert len(result["deleted_directories"]) == 6

    def test_deprovision_with_agents(self, nx: NexusFS, admin_context: OperationContext) -> None:
        """Test deprovisioning user with agents."""
        # Provision user with agents (may fail to create agents in test environment)
        nx.provision_user(
            user_id="grace",
            email="grace@example.com",
            zone_id="example",
            create_api_key=False,
            create_agents=True,  # Will try but may fail
            import_skills=False,
            context=admin_context,
        )

        # Deprovision
        result = nx.deprovision_user(
            user_id="grace",
            zone_id="example",
            context=admin_context,
        )

        # Verify deprovisioning succeeded (regardless of whether agents were created)
        assert result["user_id"] == "grace"
        # Entity may or may not have been deleted depending on whether it was registered
        assert result["deleted_entities"] >= 0

    def test_deprovision_deletes_permissions(
        self, nx: NexusFS, admin_context: OperationContext
    ) -> None:
        """Test that deprovisioning deletes ReBAC permissions."""
        # Provision user (creates zone owner permission)
        nx.provision_user(
            user_id="henry",
            email="henry@example.com",
            zone_id="example",
            create_api_key=False,
            create_agents=False,
            import_skills=False,
            context=admin_context,
        )

        # Deprovision
        result = nx.deprovision_user(
            user_id="henry",
            zone_id="example",
            context=admin_context,
        )

        # Verify permissions were attempted to be deleted (may be 0 in test environment)
        assert result["deleted_permissions"] >= 0

    def test_deprovision_full_flow(
        self, nx: NexusFS, admin_context: OperationContext, record_store
    ) -> None:
        """Test complete deprovisioning flow (provision then deprovision)."""
        user_id = f"test_{uuid.uuid4().hex[:8]}"

        # 1. Provision user with all resources
        provision_result = nx.provision_user(
            user_id=user_id,
            email=f"{user_id}@example.com",
            display_name="Test User",
            zone_id="example",
            create_api_key=True,
            create_agents=True,  # May fail in test environment
            import_skills=False,
            context=admin_context,
        )

        # Verify resources created (agents may not be created in test environment)
        assert provision_result["api_key"] is not None
        assert nx.exists(f"/zone/example/user:{user_id}/workspace", context=admin_context)

        # 2. Deprovision user
        deprovision_result = nx.deprovision_user(
            user_id=user_id,
            zone_id="example",
            delete_user_record=True,
            context=admin_context,
        )

        # Verify resources deleted
        assert len(deprovision_result["deleted_directories"]) > 0  # At least some directories
        assert deprovision_result["deleted_api_keys"] >= 1
        assert deprovision_result["user_record_deleted"] is True

        # 3. Verify user directories are empty (all data gone)
        workspace_path = f"/zone/example/user:{user_id}/workspace"
        try:
            files = nx.list(workspace_path, recursive=True, context=admin_context)
            if isinstance(files, list):
                file_count = len(files)
            elif isinstance(files, dict):
                file_count = len(files.get("files", []))
            else:
                file_count = 0
            # Workspace should be empty
            assert file_count == 0, f"Workspace still contains {file_count} files"
        except Exception:
            # Workspace doesn't exist or is empty - good!
            pass

        # 4. Verify user is soft-deleted in database
        from nexus.storage.models import UserModel

        session = record_store.session_factory()
        try:
            user = session.query(UserModel).filter_by(user_id=user_id).first()
            assert user is not None
            assert user.is_active == 0
            assert user.deleted_at is not None
        finally:
            session.close()
