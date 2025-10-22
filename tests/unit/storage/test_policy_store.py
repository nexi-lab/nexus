"""Tests for PolicyStore."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from nexus.core.permission_policy import PermissionPolicy
from nexus.storage.models import Base
from nexus.storage.policy_store import PolicyStore


@pytest.fixture
def session() -> Session:
    """Create an in-memory SQLite session for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def policy_store(session: Session) -> PolicyStore:
    """Create a PolicyStore instance."""
    return PolicyStore(session)


@pytest.fixture
def sample_policy() -> PermissionPolicy:
    """Create a sample policy for testing."""
    return PermissionPolicy(
        policy_id="test-policy",
        namespace_pattern="/workspaces/*",
        tenant_id="tenant1",
        default_owner="user1",
        default_group="group1",
        default_mode=0o755,
        priority=100,
    )


class TestPolicyStoreBasicOperations:
    """Test basic CRUD operations."""

    def test_create_policy(
        self, policy_store: PolicyStore, sample_policy: PermissionPolicy
    ) -> None:
        """Test creating a policy."""
        policy_store.create_policy(sample_policy)

        # Verify it was created
        retrieved = policy_store.get_policy("test-policy")
        assert retrieved is not None
        assert retrieved.policy_id == "test-policy"
        assert retrieved.namespace_pattern == "/workspaces/*"
        assert retrieved.tenant_id == "tenant1"
        assert retrieved.default_owner == "user1"
        assert retrieved.default_group == "group1"
        assert retrieved.default_mode == 0o755
        assert retrieved.priority == 100

    def test_get_nonexistent_policy(self, policy_store: PolicyStore) -> None:
        """Test getting a policy that doesn't exist."""
        result = policy_store.get_policy("nonexistent")
        assert result is None

    def test_update_policy(
        self, policy_store: PolicyStore, sample_policy: PermissionPolicy
    ) -> None:
        """Test updating an existing policy."""
        # Create initial policy
        policy_store.create_policy(sample_policy)

        # Update it
        updated_policy = PermissionPolicy(
            policy_id="test-policy",
            namespace_pattern="/updated/*",
            tenant_id="tenant2",
            default_owner="user2",
            default_group="group2",
            default_mode=0o644,
            priority=200,
        )
        policy_store.update_policy(updated_policy)

        # Verify changes
        retrieved = policy_store.get_policy("test-policy")
        assert retrieved is not None
        assert retrieved.namespace_pattern == "/updated/*"
        assert retrieved.tenant_id == "tenant2"
        assert retrieved.default_owner == "user2"
        assert retrieved.default_group == "group2"
        assert retrieved.default_mode == 0o644
        assert retrieved.priority == 200

    def test_update_nonexistent_policy(
        self, policy_store: PolicyStore, sample_policy: PermissionPolicy
    ) -> None:
        """Test updating a policy that doesn't exist."""
        with pytest.raises(ValueError, match="Policy not found"):
            policy_store.update_policy(sample_policy)

    def test_delete_policy(
        self, policy_store: PolicyStore, sample_policy: PermissionPolicy
    ) -> None:
        """Test deleting a policy."""
        # Create policy
        policy_store.create_policy(sample_policy)

        # Delete it
        policy_store.delete_policy("test-policy")

        # Verify it's gone
        result = policy_store.get_policy("test-policy")
        assert result is None

    def test_delete_nonexistent_policy(self, policy_store: PolicyStore) -> None:
        """Test deleting a policy that doesn't exist (should not raise)."""
        # Should not raise an error
        policy_store.delete_policy("nonexistent")


class TestPolicyStoreListOperations:
    """Test list and query operations."""

    def test_list_policies_empty(self, policy_store: PolicyStore) -> None:
        """Test listing policies when none exist."""
        policies = policy_store.list_policies()
        assert policies == []

    def test_list_policies_multiple(self, policy_store: PolicyStore) -> None:
        """Test listing multiple policies."""
        # Create multiple policies
        policy1 = PermissionPolicy(
            policy_id="policy1",
            namespace_pattern="/workspace1/*",
            tenant_id="tenant1",
            default_owner="user1",
            default_group="group1",
            default_mode=0o755,
            priority=100,
        )
        policy2 = PermissionPolicy(
            policy_id="policy2",
            namespace_pattern="/workspace2/*",
            tenant_id="tenant1",
            default_owner="user2",
            default_group="group2",
            default_mode=0o644,
            priority=200,
        )
        policy3 = PermissionPolicy(
            policy_id="policy3",
            namespace_pattern="/workspace3/*",
            tenant_id="tenant2",
            default_owner="user3",
            default_group="group3",
            default_mode=0o700,
            priority=50,
        )

        policy_store.create_policy(policy1)
        policy_store.create_policy(policy2)
        policy_store.create_policy(policy3)

        # List all policies
        all_policies = policy_store.list_policies()
        assert len(all_policies) == 3
        # Should be ordered by priority descending
        assert all_policies[0].policy_id == "policy2"  # priority 200
        assert all_policies[1].policy_id == "policy1"  # priority 100
        assert all_policies[2].policy_id == "policy3"  # priority 50

    def test_list_policies_filtered_by_tenant(self, policy_store: PolicyStore) -> None:
        """Test listing policies filtered by tenant."""
        # Create policies for different tenants
        policy1 = PermissionPolicy(
            policy_id="policy1",
            namespace_pattern="/workspace1/*",
            tenant_id="tenant1",
            default_owner="user1",
            default_group="group1",
            default_mode=0o755,
            priority=100,
        )
        policy2 = PermissionPolicy(
            policy_id="policy2",
            namespace_pattern="/workspace2/*",
            tenant_id="tenant2",
            default_owner="user2",
            default_group="group2",
            default_mode=0o644,
            priority=200,
        )
        policy3 = PermissionPolicy(
            policy_id="policy3",
            namespace_pattern="/workspace3/*",
            tenant_id=None,  # System-wide policy
            default_owner="admin",
            default_group="admin",
            default_mode=0o755,
            priority=50,
        )

        policy_store.create_policy(policy1)
        policy_store.create_policy(policy2)
        policy_store.create_policy(policy3)

        # List policies for tenant1 (should include system-wide)
        tenant1_policies = policy_store.list_policies(tenant_id="tenant1")
        assert len(tenant1_policies) == 2
        policy_ids = {p.policy_id for p in tenant1_policies}
        assert policy_ids == {"policy1", "policy3"}

    def test_find_policies_for_path(self, policy_store: PolicyStore) -> None:
        """Test finding policies that match a path."""
        # Create policies with different patterns
        policy1 = PermissionPolicy(
            policy_id="policy1",
            namespace_pattern="/workspaces/*",
            tenant_id="tenant1",
            default_owner="user1",
            default_group="group1",
            default_mode=0o755,
            priority=100,
        )
        policy2 = PermissionPolicy(
            policy_id="policy2",
            namespace_pattern="/workspaces/project1/*",
            tenant_id="tenant1",
            default_owner="user2",
            default_group="group2",
            default_mode=0o644,
            priority=200,
        )
        policy3 = PermissionPolicy(
            policy_id="policy3",
            namespace_pattern="/data/*",
            tenant_id="tenant1",
            default_owner="user3",
            default_group="group3",
            default_mode=0o700,
            priority=50,
        )

        policy_store.create_policy(policy1)
        policy_store.create_policy(policy2)
        policy_store.create_policy(policy3)

        # Find policies for a path under /workspaces/project1
        matching = policy_store.find_policies_for_path(
            "/workspaces/project1/file.txt", tenant_id="tenant1"
        )
        assert len(matching) == 2
        policy_ids = {p.policy_id for p in matching}
        assert policy_ids == {"policy1", "policy2"}

        # Find policies for a path under /data
        matching = policy_store.find_policies_for_path("/data/file.txt", tenant_id="tenant1")
        assert len(matching) == 1
        assert matching[0].policy_id == "policy3"

        # Find policies for a path that doesn't match
        matching = policy_store.find_policies_for_path("/other/file.txt", tenant_id="tenant1")
        assert len(matching) == 0


class TestPolicyStoreValidation:
    """Test policy validation."""

    def test_validate_missing_policy_id(self, policy_store: PolicyStore) -> None:
        """Test validation fails for missing policy_id."""
        policy = PermissionPolicy(
            policy_id="",
            namespace_pattern="/workspace/*",
            tenant_id="tenant1",
            default_owner="user1",
            default_group="group1",
            default_mode=0o755,
            priority=100,
        )

        with pytest.raises(ValueError, match="policy_id is required"):
            policy_store.create_policy(policy)

    def test_validate_missing_namespace_pattern(self, policy_store: PolicyStore) -> None:
        """Test validation fails for missing namespace_pattern."""
        policy = PermissionPolicy(
            policy_id="test-policy",
            namespace_pattern="",
            tenant_id="tenant1",
            default_owner="user1",
            default_group="group1",
            default_mode=0o755,
            priority=100,
        )

        with pytest.raises(ValueError, match="namespace_pattern is required"):
            policy_store.create_policy(policy)

    def test_validate_missing_default_owner(self, policy_store: PolicyStore) -> None:
        """Test validation fails for missing default_owner."""
        policy = PermissionPolicy(
            policy_id="test-policy",
            namespace_pattern="/workspace/*",
            tenant_id="tenant1",
            default_owner="",
            default_group="group1",
            default_mode=0o755,
            priority=100,
        )

        with pytest.raises(ValueError, match="default_owner is required"):
            policy_store.create_policy(policy)

    def test_validate_missing_default_group(self, policy_store: PolicyStore) -> None:
        """Test validation fails for missing default_group."""
        policy = PermissionPolicy(
            policy_id="test-policy",
            namespace_pattern="/workspace/*",
            tenant_id="tenant1",
            default_owner="user1",
            default_group="",
            default_mode=0o755,
            priority=100,
        )

        with pytest.raises(ValueError, match="default_group is required"):
            policy_store.create_policy(policy)

    def test_validate_invalid_mode_too_high(self, policy_store: PolicyStore) -> None:
        """Test validation fails for mode > 0o777."""
        policy = PermissionPolicy(
            policy_id="test-policy",
            namespace_pattern="/workspace/*",
            tenant_id="tenant1",
            default_owner="user1",
            default_group="group1",
            default_mode=0o1000,  # Invalid
            priority=100,
        )

        with pytest.raises(ValueError, match="default_mode must be between"):
            policy_store.create_policy(policy)

    def test_validate_invalid_mode_negative(self, policy_store: PolicyStore) -> None:
        """Test validation fails for negative mode."""
        policy = PermissionPolicy(
            policy_id="test-policy",
            namespace_pattern="/workspace/*",
            tenant_id="tenant1",
            default_owner="user1",
            default_group="group1",
            default_mode=-1,  # Invalid
            priority=100,
        )

        with pytest.raises(ValueError, match="default_mode must be between"):
            policy_store.create_policy(policy)

    def test_validate_negative_priority(self, policy_store: PolicyStore) -> None:
        """Test validation fails for negative priority."""
        policy = PermissionPolicy(
            policy_id="test-policy",
            namespace_pattern="/workspace/*",
            tenant_id="tenant1",
            default_owner="user1",
            default_group="group1",
            default_mode=0o755,
            priority=-1,  # Invalid
        )

        with pytest.raises(ValueError, match="priority must be non-negative"):
            policy_store.create_policy(policy)

    def test_validate_on_update(self, policy_store: PolicyStore) -> None:
        """Test validation also applies to updates."""
        # Create valid policy first
        policy = PermissionPolicy(
            policy_id="test-policy",
            namespace_pattern="/workspace/*",
            tenant_id="tenant1",
            default_owner="user1",
            default_group="group1",
            default_mode=0o755,
            priority=100,
        )
        policy_store.create_policy(policy)

        # Try to update with invalid data
        invalid_policy = PermissionPolicy(
            policy_id="test-policy",
            namespace_pattern="",  # Invalid
            tenant_id="tenant1",
            default_owner="user1",
            default_group="group1",
            default_mode=0o755,
            priority=100,
        )

        with pytest.raises(ValueError, match="namespace_pattern is required"):
            policy_store.update_policy(invalid_policy)
