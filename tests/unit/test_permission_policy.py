"""Unit tests for permission policy system."""

import uuid

from nexus.core.permission_policy import (
    PermissionPolicy,
    PolicyMatcher,
    create_default_policies,
)


class TestPermissionPolicy:
    """Test PermissionPolicy class."""

    def test_matches_exact_path(self):
        """Test exact path matching."""
        policy = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/data.txt",
            tenant_id=None,
            default_owner="alice",
            default_group="users",
            default_mode=0o644,
        )

        assert policy.matches("/workspace/data.txt")
        assert not policy.matches("/workspace/other.txt")

    def test_matches_glob_pattern(self):
        """Test glob pattern matching."""
        policy = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/*",
            tenant_id=None,
            default_owner="alice",
            default_group="users",
            default_mode=0o644,
        )

        assert policy.matches("/workspace/file.txt")
        assert policy.matches("/workspace/data.json")
        assert not policy.matches("/shared/file.txt")
        # Note: fnmatch's * matches any characters including /
        # For recursive patterns, the actual default policies use /workspace/* which works fine
        assert policy.matches("/workspace/sub/file.txt")  # * matches any characters in fnmatch

    def test_matches_recursive_pattern(self):
        """Test recursive glob pattern matching."""
        policy = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/**",
            tenant_id=None,
            default_owner="alice",
            default_group="users",
            default_mode=0o644,
        )

        assert policy.matches("/workspace/file.txt")
        assert policy.matches("/workspace/sub/file.txt")
        assert policy.matches("/workspace/deep/nested/file.txt")
        assert not policy.matches("/shared/file.txt")

    def test_apply_no_variables(self):
        """Test applying policy without variable substitution."""
        policy = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/shared/*",
            tenant_id=None,
            default_owner="root",
            default_group="shared",
            default_mode=0o664,
        )

        owner, group, mode = policy.apply()

        assert owner == "root"
        assert group == "shared"
        assert mode == 0o664

    def test_apply_with_agent_id_substitution(self):
        """Test applying policy with ${agent_id} substitution."""
        policy = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/*",
            tenant_id=None,
            default_owner="${agent_id}",
            default_group="agents",
            default_mode=0o644,
        )

        context = {"agent_id": "alice"}
        owner, group, mode = policy.apply(context)

        assert owner == "alice"
        assert group == "agents"
        assert mode == 0o644

    def test_apply_with_tenant_id_substitution(self):
        """Test applying policy with ${tenant_id} substitution."""
        policy = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/shared/*",
            tenant_id=None,
            default_owner="root",
            default_group="${tenant_id}",
            default_mode=0o664,
        )

        context = {"tenant_id": "acme-corp"}
        owner, group, mode = policy.apply(context)

        assert owner == "root"
        assert group == "acme-corp"
        assert mode == 0o664

    def test_apply_with_missing_variable(self):
        """Test applying policy with missing variable in context."""
        policy = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/*",
            tenant_id=None,
            default_owner="${agent_id}",
            default_group="agents",
            default_mode=0o644,
        )

        # Variable not in context - should keep placeholder
        owner, group, mode = policy.apply({})

        assert owner == "${agent_id}"  # Unchanged
        assert group == "agents"
        assert mode == 0o644


class TestPolicyMatcher:
    """Test PolicyMatcher class."""

    def test_find_matching_policy_single_match(self):
        """Test finding a single matching policy."""
        policy1 = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/*",
            tenant_id=None,
            default_owner="alice",
            default_group="users",
            default_mode=0o644,
            priority=10,
        )

        matcher = PolicyMatcher([policy1])
        result = matcher.find_matching_policy("/workspace/file.txt")

        assert result is not None
        assert result.policy_id == policy1.policy_id

    def test_find_matching_policy_multiple_matches(self):
        """Test finding policy with multiple matches (highest priority wins)."""
        policy1 = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/*",
            tenant_id=None,
            default_owner="alice",
            default_group="users",
            default_mode=0o644,
            priority=10,
        )

        policy2 = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/**",
            tenant_id=None,
            default_owner="bob",
            default_group="admins",
            default_mode=0o600,
            priority=20,  # Higher priority
        )

        matcher = PolicyMatcher([policy1, policy2])
        result = matcher.find_matching_policy("/workspace/file.txt")

        # policy2 should win (higher priority)
        assert result is not None
        assert result.policy_id == policy2.policy_id

    def test_find_matching_policy_tenant_specific_wins(self):
        """Test that tenant-specific policies take precedence over system-wide."""
        policy1 = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/*",
            tenant_id=None,  # System-wide
            default_owner="root",
            default_group="users",
            default_mode=0o644,
            priority=10,
        )

        policy2 = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/*",
            tenant_id="acme-corp",  # Tenant-specific
            default_owner="alice",
            default_group="acme",
            default_mode=0o600,
            priority=10,  # Same priority
        )

        matcher = PolicyMatcher([policy1, policy2])
        result = matcher.find_matching_policy("/workspace/file.txt", tenant_id="acme-corp")

        # Tenant-specific policy should win
        assert result is not None
        assert result.policy_id == policy2.policy_id

    def test_find_matching_policy_no_match(self):
        """Test finding policy with no matches."""
        policy1 = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/*",
            tenant_id=None,
            default_owner="alice",
            default_group="users",
            default_mode=0o644,
        )

        matcher = PolicyMatcher([policy1])
        result = matcher.find_matching_policy("/shared/file.txt")

        assert result is None

    def test_apply_policy_success(self):
        """Test applying policy successfully."""
        policy1 = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/*",
            tenant_id=None,
            default_owner="${agent_id}",
            default_group="agents",
            default_mode=0o644,
        )

        matcher = PolicyMatcher([policy1])
        context = {"agent_id": "alice"}

        result = matcher.apply_policy("/workspace/file.txt", context=context)

        assert result is not None
        owner, group, mode = result
        assert owner == "alice"
        assert group == "agents"
        assert mode == 0o644

    def test_apply_policy_no_match(self):
        """Test applying policy with no match."""
        policy1 = PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/*",
            tenant_id=None,
            default_owner="alice",
            default_group="users",
            default_mode=0o644,
        )

        matcher = PolicyMatcher([policy1])
        result = matcher.apply_policy("/shared/file.txt")

        assert result is None


class TestDefaultPolicies:
    """Test default policy creation."""

    def test_create_default_policies(self):
        """Test creating default policies for standard namespaces."""
        policies = create_default_policies()

        assert len(policies) == 4  # workspace, shared, archives, system

        # Check workspace policy
        workspace_policy = next(p for p in policies if "/workspace/" in p.namespace_pattern)
        assert workspace_policy.default_owner == "${agent_id}"
        assert workspace_policy.default_group == "agents"
        assert workspace_policy.default_mode == 0o644

        # Check shared policy
        shared_policy = next(p for p in policies if "/shared/" in p.namespace_pattern)
        assert shared_policy.default_owner == "root"
        assert shared_policy.default_group == "${tenant_id}"
        assert shared_policy.default_mode == 0o664

        # Check archives policy
        archives_policy = next(p for p in policies if "/archives/" in p.namespace_pattern)
        assert archives_policy.default_owner == "root"
        assert archives_policy.default_group == "${tenant_id}"
        assert archives_policy.default_mode == 0o444  # Read-only

        # Check system policy
        system_policy = next(p for p in policies if "/system/" in p.namespace_pattern)
        assert system_policy.default_owner == "root"
        assert system_policy.default_group == "root"
        assert system_policy.default_mode == 0o600  # Admin-only

    def test_default_policies_match_correctly(self):
        """Test that default policies match their intended paths."""
        policies = create_default_policies()
        matcher = PolicyMatcher(policies)

        # Test workspace policy
        context = {"agent_id": "alice", "tenant_id": "acme-corp"}
        result = matcher.apply_policy("/workspace/file.txt", context=context)
        assert result is not None
        owner, group, mode = result
        assert owner == "alice"
        assert group == "agents"

        # Test shared policy
        result = matcher.apply_policy("/shared/data.txt", context=context)
        assert result is not None
        owner, group, mode = result
        assert owner == "root"
        assert group == "acme-corp"
        assert mode == 0o664

        # Test archives policy
        result = matcher.apply_policy("/archives/backup.tar.gz", context=context)
        assert result is not None
        owner, group, mode = result
        assert mode == 0o444  # Read-only

        # Test system policy
        result = matcher.apply_policy("/system/config.yaml", context=context)
        assert result is not None
        owner, group, mode = result
        assert owner == "root"
        assert group == "root"
        assert mode == 0o600
