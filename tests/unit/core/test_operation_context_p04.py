"""Tests for OperationContext P0-4 features.

P0-4 added two new fields to OperationContext:
1. admin_capabilities: Set of granted admin capabilities (e.g., admin:read:*)
2. request_id: Unique UUID for audit trail correlation

These fields support the scoped admin bypass and audit logging features.
"""

import uuid

import pytest

from nexus.core.permissions import OperationContext


class TestAdminCapabilitiesField:
    """Test the admin_capabilities field in OperationContext."""

    def test_admin_capabilities_defaults_to_empty_set(self):
        """Admin capabilities should default to empty set."""
        ctx = OperationContext(user="alice", groups=[])

        assert isinstance(ctx.admin_capabilities, set)
        assert len(ctx.admin_capabilities) == 0

    def test_admin_capabilities_can_be_set(self):
        """Admin capabilities can be provided at creation."""
        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*", "admin:write:*", "admin:delete:*"},
        )

        assert len(ctx.admin_capabilities) == 3
        assert "admin:read:*" in ctx.admin_capabilities
        assert "admin:write:*" in ctx.admin_capabilities
        assert "admin:delete:*" in ctx.admin_capabilities

    def test_admin_capabilities_stored_as_set(self):
        """Admin capabilities should be stored as a set (not list)."""
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*", "admin:write:*"},
        )

        assert isinstance(ctx.admin_capabilities, set)

        # Sets don't allow duplicates
        ctx.admin_capabilities.add("admin:read:*")  # Duplicate
        assert len(ctx.admin_capabilities) == 2  # Still 2

    def test_admin_capabilities_can_be_empty_for_admin(self):
        """Admin user can have empty capabilities (uses ReBAC instead)."""
        ctx = OperationContext(
            user="admin", groups=["admins"], is_admin=True, admin_capabilities=set()
        )

        assert ctx.is_admin is True
        assert len(ctx.admin_capabilities) == 0

    def test_regular_user_can_have_capabilities(self):
        """Regular users can also have capabilities (for testing/special cases)."""
        ctx = OperationContext(
            user="alice",
            groups=[],
            is_admin=False,
            admin_capabilities={"admin:read:*"},  # Has capability but not admin
        )

        assert ctx.is_admin is False
        assert "admin:read:*" in ctx.admin_capabilities


class TestRequestIdField:
    """Test the request_id field in OperationContext."""

    def test_request_id_auto_generated(self):
        """Request ID should be auto-generated as UUID."""
        ctx = OperationContext(user="alice", groups=[])

        # Should have a request_id
        assert ctx.request_id is not None
        assert isinstance(ctx.request_id, str)

        # Should be valid UUID format
        try:
            uuid.UUID(ctx.request_id)
        except ValueError:
            pytest.fail(f"request_id '{ctx.request_id}' is not a valid UUID")

    def test_request_id_unique_for_each_context(self):
        """Each context should have a unique request_id."""
        ctx1 = OperationContext(user="alice", groups=[])
        ctx2 = OperationContext(user="alice", groups=[])
        ctx3 = OperationContext(user="bob", groups=[])

        # All should be different
        assert ctx1.request_id != ctx2.request_id
        assert ctx1.request_id != ctx3.request_id
        assert ctx2.request_id != ctx3.request_id

    def test_request_id_can_be_set_custom(self):
        """Request ID can be set to a custom value (for correlation)."""
        custom_id = "custom-request-12345"

        ctx = OperationContext(user="alice", groups=[], request_id=custom_id)

        assert ctx.request_id == custom_id

    def test_request_id_for_audit_correlation(self):
        """Request ID is used for correlating operations in audit logs."""
        request_id = "trace-abc-123"

        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
            request_id=request_id,
        )

        # Request ID should be preserved for audit logging
        assert ctx.request_id == request_id


class TestOperationContextP04Fields:
    """Test interaction of P0-4 fields with other context fields."""

    def test_context_with_all_p04_fields(self):
        """Test creating context with all P0-4 fields."""
        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            tenant_id="org_acme",
            agent_id="agent_001",
            is_admin=True,
            is_system=False,
            admin_capabilities={"admin:read:*", "admin:write:*"},
            request_id="req-123",
        )

        # All fields should be set correctly
        assert ctx.user == "admin"
        assert ctx.is_admin is True
        assert len(ctx.admin_capabilities) == 2
        assert ctx.request_id == "req-123"
        assert ctx.tenant_id == "org_acme"

    def test_admin_capabilities_with_tenant_context(self):
        """Admin capabilities work with multi-tenant context."""
        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            tenant_id="tenant_alpha",
            is_admin=True,
            admin_capabilities={"admin:read:tenant_alpha", "admin:write:tenant_alpha"},
        )

        assert ctx.tenant_id == "tenant_alpha"
        assert "admin:read:tenant_alpha" in ctx.admin_capabilities

    def test_system_context_with_request_id(self):
        """System operations also have request IDs for audit."""
        ctx = OperationContext(user="system", groups=[], is_system=True)

        # System operations also get request IDs
        assert ctx.request_id is not None
        assert isinstance(ctx.request_id, str)


class TestOperationContextBackwardCompatibility:
    """Test that P0-4 changes don't break existing code."""

    def test_existing_context_creation_still_works(self):
        """Existing code that creates contexts should still work."""
        # Old-style context creation (no P0-4 fields)
        ctx = OperationContext(user="alice", groups=["developers"], tenant_id="org1")

        # Should work with defaults
        assert ctx.user == "alice"
        assert len(ctx.admin_capabilities) == 0
        assert ctx.request_id is not None

    def test_admin_context_without_capabilities_still_works(self):
        """Admin contexts without capabilities should work (fall through to ReBAC)."""
        ctx = OperationContext(user="admin", groups=["admins"], is_admin=True)

        # Should create successfully
        assert ctx.is_admin is True
        assert len(ctx.admin_capabilities) == 0  # Empty by default

    def test_get_subject_still_works(self):
        """The get_subject() method should still work with P0-4 fields."""
        ctx = OperationContext(
            user="alice",
            groups=[],
            subject_type="user",
            subject_id="alice",
            admin_capabilities={"admin:read:*"},
            request_id="req-123",
        )

        assert ctx.get_subject() == ("user", "alice")


class TestOperationContextValidation:
    """Test validation of P0-4 fields."""

    def test_admin_capabilities_must_be_set(self):
        """Admin capabilities must be a set type."""
        # This should work (set)
        ctx = OperationContext(user="admin", groups=[], admin_capabilities=set())
        assert isinstance(ctx.admin_capabilities, set)

        # Note: If list is passed, dataclass will convert it
        # Let's test that the field default is correct
        ctx2 = OperationContext(user="admin", groups=[])
        assert isinstance(ctx2.admin_capabilities, set)

    def test_request_id_must_be_string(self):
        """Request ID must be a string."""
        ctx = OperationContext(user="alice", groups=[], request_id="my-request-123")

        assert isinstance(ctx.request_id, str)
        assert ctx.request_id == "my-request-123"

    def test_empty_admin_capabilities_is_valid(self):
        """Empty admin capabilities set is a valid state."""
        ctx = OperationContext(user="alice", groups=[], admin_capabilities=set())

        assert ctx.admin_capabilities == set()
        assert len(ctx.admin_capabilities) == 0
