"""Tests for ReBACService.

Covers the security-critical Relationship-Based Access Control service:
- Core ReBAC operations (create, check, expand, delete tuples)
- Batch permission checking
- Configuration and namespace management
- Input validation and error handling
- Zone-scoped operations
- Permission enforcement (share permission checks)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from nexus.core.permissions import OperationContext
from nexus.services.rebac_service import ReBACService

# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def mock_rebac_manager():
    """Create a mock EnhancedReBACManager with standard return values."""
    mock = MagicMock()
    # rebac_write returns a WriteResult-like object
    write_result = MagicMock()
    write_result.tuple_id = "tuple-123"
    write_result.revision = 42
    write_result.consistency_token = "token-abc"
    mock.rebac_write.return_value = write_result
    # rebac_check returns bool
    mock.rebac_check.return_value = True
    # rebac_expand returns list of subject tuples
    mock.rebac_expand.return_value = [("user", "alice"), ("user", "bob")]
    # rebac_explain returns dict
    mock.rebac_explain.return_value = {
        "result": True,
        "cached": False,
        "reason": "Direct relationship: owner",
        "paths": [{"type": "direct", "relation": "owner"}],
        "successful_path": {"type": "direct", "relation": "owner"},
    }
    # rebac_delete returns bool
    mock.rebac_delete.return_value = True
    # rebac_check_batch_fast returns list of bools
    mock.rebac_check_batch_fast.return_value = [True, False, True]
    # Configuration attributes
    mock.max_depth = 10
    mock.cache_ttl_seconds = 300
    return mock


@pytest.fixture
def service(mock_rebac_manager):
    """Create ReBACService with mocked manager and permissions disabled."""
    return ReBACService(
        rebac_manager=mock_rebac_manager,
        enforce_permissions=False,
        enable_audit_logging=True,
    )


@pytest.fixture
def enforced_service(mock_rebac_manager):
    """Create ReBACService with permission enforcement enabled."""
    return ReBACService(
        rebac_manager=mock_rebac_manager,
        enforce_permissions=True,
        enable_audit_logging=True,
    )


@pytest.fixture
def no_manager_service():
    """Create ReBACService without a ReBAC manager."""
    return ReBACService(
        rebac_manager=None,
        enforce_permissions=False,
    )


# =========================================================================
# Initialization Tests
# =========================================================================


class TestReBACServiceInit:
    """Test ReBACService initialization."""

    def test_init_with_manager(self, mock_rebac_manager):
        """Test initialization with all dependencies."""
        svc = ReBACService(
            rebac_manager=mock_rebac_manager,
            enforce_permissions=True,
            enable_audit_logging=True,
        )
        assert svc._rebac_manager is mock_rebac_manager
        assert svc._enforce_permissions is True
        assert svc._enable_audit_logging is True

    def test_init_without_manager(self):
        """Test initialization without manager (lazy init pattern)."""
        svc = ReBACService(rebac_manager=None, enforce_permissions=False)
        assert svc._rebac_manager is None
        assert svc._enforce_permissions is False

    def test_init_defaults(self, mock_rebac_manager):
        """Test that defaults are sensible for production use."""
        svc = ReBACService(rebac_manager=mock_rebac_manager)
        assert svc._enforce_permissions is True
        assert svc._enable_audit_logging is True


# =========================================================================
# rebac_create Tests
# =========================================================================


class TestReBACCreate:
    """Test rebac_create - the core permission granting operation."""

    @pytest.mark.asyncio
    async def test_create_basic_tuple(self, service, mock_rebac_manager):
        """Test creating a simple relationship tuple."""
        result = await service.rebac_create(
            subject=("user", "alice"),
            relation="owner",
            object=("file", "/doc.txt"),
        )

        assert result["tuple_id"] == "tuple-123"
        assert result["revision"] == 42
        assert result["consistency_token"] == "token-abc"
        mock_rebac_manager.rebac_write.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_with_zone_id(self, service, mock_rebac_manager):
        """Test creating tuple with explicit zone isolation."""
        await service.rebac_create(
            subject=("user", "alice"),
            relation="can-read",
            object=("file", "/doc.txt"),
            zone_id="zone-acme",
        )

        call_kwargs = mock_rebac_manager.rebac_write.call_args[1]
        assert call_kwargs["zone_id"] == "zone-acme"

    @pytest.mark.asyncio
    async def test_create_extracts_zone_from_dict_context(self, service, mock_rebac_manager):
        """Test that zone_id is extracted from dict context when not explicit."""
        await service.rebac_create(
            subject=("user", "alice"),
            relation="can-read",
            object=("file", "/doc.txt"),
            context={"zone": "zone-from-ctx"},
        )

        call_kwargs = mock_rebac_manager.rebac_write.call_args[1]
        assert call_kwargs["zone_id"] == "zone-from-ctx"

    @pytest.mark.asyncio
    async def test_create_extracts_zone_from_operation_context(
        self, service, mock_rebac_manager, operation_context
    ):
        """Test that zone_id is extracted from OperationContext."""
        await service.rebac_create(
            subject=("user", "alice"),
            relation="can-read",
            object=("file", "/doc.txt"),
            context=operation_context,
        )

        call_kwargs = mock_rebac_manager.rebac_write.call_args[1]
        assert call_kwargs["zone_id"] == "test_zone"

    @pytest.mark.asyncio
    async def test_create_with_expiration(self, service, mock_rebac_manager):
        """Test creating a temporary relationship with expiration."""
        expires = datetime.now() + timedelta(hours=24)
        await service.rebac_create(
            subject=("user", "bob"),
            relation="can-read",
            object=("file", "/doc.txt"),
            expires_at=expires,
        )

        call_kwargs = mock_rebac_manager.rebac_write.call_args[1]
        assert call_kwargs["expires_at"] == expires

    @pytest.mark.asyncio
    async def test_create_with_3_tuple_subject(self, service, mock_rebac_manager):
        """Test creating tuple with userset-as-subject (3-tuple)."""
        result = await service.rebac_create(
            subject=("group", "developers", "member"),
            relation="can-read",
            object=("file", "/shared.txt"),
        )

        assert result["tuple_id"] == "tuple-123"
        call_kwargs = mock_rebac_manager.rebac_write.call_args[1]
        assert call_kwargs["subject"] == ("group", "developers", "member")

    @pytest.mark.asyncio
    async def test_create_raises_without_manager(self, no_manager_service):
        """Test that create raises RuntimeError without manager."""
        with pytest.raises(RuntimeError, match="ReBAC manager is not available"):
            await no_manager_service.rebac_create(
                subject=("user", "alice"),
                relation="owner",
                object=("file", "/doc.txt"),
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "invalid_subject",
        [
            "not-a-tuple",
            ("single",),
            ("a", "b", "c", "d"),
        ],
        ids=["string", "1-tuple", "4-tuple"],
    )
    async def test_create_rejects_invalid_subject(self, service, invalid_subject):
        """Test that invalid subject tuples are rejected."""
        with pytest.raises(ValueError, match="subject must be"):
            await service.rebac_create(
                subject=invalid_subject,
                relation="owner",
                object=("file", "/doc.txt"),
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "invalid_object",
        [
            "not-a-tuple",
            ("single",),
            ("a", "b", "c"),
        ],
        ids=["string", "1-tuple", "3-tuple"],
    )
    async def test_create_rejects_invalid_object(self, service, invalid_object):
        """Test that invalid object tuples are rejected."""
        with pytest.raises(ValueError, match="object must be"):
            await service.rebac_create(
                subject=("user", "alice"),
                relation="owner",
                object=invalid_object,
            )

    @pytest.mark.asyncio
    async def test_create_dynamic_viewer_requires_csv(self, service):
        """Test dynamic_viewer relation only works with CSV files."""
        with pytest.raises(ValueError, match="dynamic_viewer relation only supports CSV"):
            await service.rebac_create(
                subject=("user", "alice"),
                relation="dynamic_viewer",
                object=("file", "/doc.txt"),
                column_config={"hidden_columns": ["password"]},
            )

    @pytest.mark.asyncio
    async def test_create_dynamic_viewer_requires_column_config(self, service):
        """Test dynamic_viewer relation requires column_config."""
        with pytest.raises(ValueError, match="column_config is required"):
            await service.rebac_create(
                subject=("user", "alice"),
                relation="dynamic_viewer",
                object=("file", "/data.csv"),
            )

    @pytest.mark.asyncio
    async def test_create_dynamic_viewer_validates_column_overlap(self, service):
        """Test that columns cannot appear in multiple categories."""
        with pytest.raises(ValueError, match="appears in multiple categories"):
            await service.rebac_create(
                subject=("user", "alice"),
                relation="dynamic_viewer",
                object=("file", "/data.csv"),
                column_config={
                    "hidden_columns": ["email"],
                    "visible_columns": ["email"],  # duplicate!
                },
            )

    @pytest.mark.asyncio
    async def test_create_dynamic_viewer_validates_aggregation_ops(self, service):
        """Test that invalid aggregation operations are rejected."""
        with pytest.raises(ValueError, match="Invalid aggregation operation"):
            await service.rebac_create(
                subject=("user", "alice"),
                relation="dynamic_viewer",
                object=("file", "/data.csv"),
                column_config={
                    "aggregations": {"age": "invalid_op"},
                },
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "agg_op",
        ["mean", "sum", "min", "max", "std", "median", "count"],
    )
    async def test_create_dynamic_viewer_accepts_valid_aggregation_ops(
        self, service, mock_rebac_manager, agg_op
    ):
        """Test that all valid aggregation operations are accepted."""
        result = await service.rebac_create(
            subject=("user", "alice"),
            relation="dynamic_viewer",
            object=("file", "/data.csv"),
            column_config={
                "aggregations": {"age": agg_op},
            },
        )

        assert result["tuple_id"] == "tuple-123"

    @pytest.mark.asyncio
    async def test_create_column_config_only_for_dynamic_viewer(self, service):
        """Test that column_config is rejected for non-dynamic_viewer relations."""
        with pytest.raises(ValueError, match="column_config can only be provided"):
            await service.rebac_create(
                subject=("user", "alice"),
                relation="can-read",
                object=("file", "/data.csv"),
                column_config={"hidden_columns": ["password"]},
            )


# =========================================================================
# rebac_check Tests
# =========================================================================


class TestReBACCheck:
    """Test rebac_check - the core permission checking operation."""

    @pytest.mark.asyncio
    async def test_check_permission_granted(self, service, mock_rebac_manager):
        """Test that check returns True when permission is granted."""
        mock_rebac_manager.rebac_check.return_value = True
        result = await service.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_check_permission_denied(self, service, mock_rebac_manager):
        """Test that check returns False when permission is denied."""
        mock_rebac_manager.rebac_check.return_value = False
        result = await service.rebac_check(
            subject=("user", "alice"),
            permission="write",
            object=("file", "/doc.txt"),
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_check_raises_without_manager(self, no_manager_service):
        """Test that check raises RuntimeError without manager."""
        with pytest.raises(RuntimeError, match="ReBAC manager is not available"):
            await no_manager_service.rebac_check(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/doc.txt"),
            )

    @pytest.mark.asyncio
    async def test_check_rejects_invalid_subject(self, service):
        """Test that check rejects invalid subject tuples."""
        with pytest.raises(ValueError, match="subject must be"):
            await service.rebac_check(
                subject="not-a-tuple",
                permission="read",
                object=("file", "/doc.txt"),
            )

    @pytest.mark.asyncio
    async def test_check_rejects_invalid_object(self, service):
        """Test that check rejects invalid object tuples."""
        with pytest.raises(ValueError, match="object must be"):
            await service.rebac_check(
                subject=("user", "alice"),
                permission="read",
                object=("single",),
            )

    @pytest.mark.asyncio
    async def test_check_with_zone_from_context(self, service, mock_rebac_manager):
        """Test that zone is extracted from OperationContext."""
        ctx = OperationContext(
            user="alice", groups=[], zone_id="zone-acme", is_system=False, is_admin=False
        )
        await service.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
            context=ctx,
        )

        call_kwargs = mock_rebac_manager.rebac_check.call_args[1]
        assert call_kwargs["zone_id"] == "zone-acme"

    @pytest.mark.asyncio
    async def test_check_explicit_zone_overrides_context(self, service, mock_rebac_manager):
        """Test that explicit zone_id takes precedence over context."""
        ctx = OperationContext(
            user="alice", groups=[], zone_id="zone-context", is_system=False, is_admin=False
        )
        await service.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
            context=ctx,
            zone_id="zone-explicit",
        )

        call_kwargs = mock_rebac_manager.rebac_check.call_args[1]
        assert call_kwargs["zone_id"] == "zone-explicit"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "permission",
        ["read", "write", "owner", "execute", "can-read", "can-write"],
    )
    async def test_check_various_permission_types(self, service, mock_rebac_manager, permission):
        """Test that various permission types are passed through correctly."""
        await service.rebac_check(
            subject=("user", "alice"),
            permission=permission,
            object=("file", "/doc.txt"),
        )

        call_kwargs = mock_rebac_manager.rebac_check.call_args[1]
        assert call_kwargs["permission"] == permission

    @pytest.mark.asyncio
    async def test_check_with_consistency_fully_consistent(self, service, mock_rebac_manager):
        """Test permission check with fully_consistent mode (security audit)."""
        await service.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
            consistency_mode="fully_consistent",
        )

        call_kwargs = mock_rebac_manager.rebac_check.call_args[1]
        assert call_kwargs["consistency"] is not None

    @pytest.mark.asyncio
    async def test_check_with_consistency_at_least_as_fresh(self, service, mock_rebac_manager):
        """Test permission check with at_least_as_fresh mode (read-after-write)."""
        await service.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
            consistency_mode="at_least_as_fresh",
            min_revision=42,
        )

        call_kwargs = mock_rebac_manager.rebac_check.call_args[1]
        assert call_kwargs["consistency"] is not None


# =========================================================================
# rebac_check_batch Tests
# =========================================================================


class TestReBACCheckBatch:
    """Test rebac_check_batch - efficient bulk permission checks."""

    @pytest.mark.asyncio
    async def test_batch_check_returns_results(self, service, mock_rebac_manager):
        """Test that batch check returns correct number of results."""
        checks = [
            (("user", "alice"), "read", ("file", "/a.txt")),
            (("user", "alice"), "read", ("file", "/b.txt")),
            (("user", "alice"), "write", ("file", "/c.txt")),
        ]
        results = await service.rebac_check_batch(checks=checks)
        assert results == [True, False, True]
        mock_rebac_manager.rebac_check_batch_fast.assert_called_once_with(checks=checks)

    @pytest.mark.asyncio
    async def test_batch_check_raises_without_manager(self, no_manager_service):
        """Test that batch check raises RuntimeError without manager."""
        checks = [(("user", "alice"), "read", ("file", "/a.txt"))]
        with pytest.raises(RuntimeError, match="ReBAC manager is not available"):
            await no_manager_service.rebac_check_batch(checks=checks)

    @pytest.mark.asyncio
    async def test_batch_check_validates_check_format(self, service):
        """Test that individual check tuples are validated."""
        bad_checks = [("not", "enough")]
        with pytest.raises(ValueError, match="Check 0 must be"):
            await service.rebac_check_batch(checks=bad_checks)

    @pytest.mark.asyncio
    async def test_batch_check_validates_subject_format(self, service):
        """Test that subject in each check is validated."""
        bad_checks = [("invalid-subject", "read", ("file", "/a.txt"))]
        with pytest.raises(ValueError, match="Check 0: subject must be"):
            await service.rebac_check_batch(checks=bad_checks)

    @pytest.mark.asyncio
    async def test_batch_check_validates_object_format(self, service):
        """Test that object in each check is validated."""
        bad_checks = [(("user", "alice"), "read", "invalid-object")]
        with pytest.raises(ValueError, match="Check 0: object must be"):
            await service.rebac_check_batch(checks=bad_checks)

    @pytest.mark.asyncio
    async def test_batch_check_empty_list(self, service, mock_rebac_manager):
        """Test that empty checks list is handled gracefully."""
        mock_rebac_manager.rebac_check_batch_fast.return_value = []
        results = await service.rebac_check_batch(checks=[])
        assert results == []


# =========================================================================
# rebac_expand Tests
# =========================================================================


class TestReBACExpand:
    """Test rebac_expand - find all subjects with a permission."""

    @pytest.mark.asyncio
    async def test_expand_returns_subjects(self, service, mock_rebac_manager):
        """Test expanding permissions returns subject list."""
        result = await service.rebac_expand(
            permission="read",
            object=("file", "/doc.txt"),
        )
        assert result == [("user", "alice"), ("user", "bob")]

    @pytest.mark.asyncio
    async def test_expand_raises_without_manager(self, no_manager_service):
        """Test that expand raises RuntimeError without manager."""
        with pytest.raises(RuntimeError, match="ReBAC manager is not available"):
            await no_manager_service.rebac_expand(
                permission="read",
                object=("file", "/doc.txt"),
            )

    @pytest.mark.asyncio
    async def test_expand_rejects_invalid_object(self, service):
        """Test that expand rejects invalid object tuples."""
        with pytest.raises(ValueError, match="object must be"):
            await service.rebac_expand(
                permission="read",
                object="not-a-tuple",
            )


# =========================================================================
# rebac_explain Tests
# =========================================================================


class TestReBACExplain:
    """Test rebac_explain - debugging API for permission checks."""

    @pytest.mark.asyncio
    async def test_explain_returns_explanation(self, service, mock_rebac_manager):
        """Test that explain returns detailed explanation."""
        result = await service.rebac_explain(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
        )
        assert result["result"] is True
        assert result["reason"] == "Direct relationship: owner"
        assert result["successful_path"] is not None

    @pytest.mark.asyncio
    async def test_explain_raises_without_manager(self, no_manager_service):
        """Test that explain raises RuntimeError without manager."""
        with pytest.raises(RuntimeError, match="ReBAC manager is not available"):
            await no_manager_service.rebac_explain(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/doc.txt"),
            )

    @pytest.mark.asyncio
    async def test_explain_rejects_invalid_subject(self, service):
        """Test that explain rejects invalid subject tuples."""
        with pytest.raises(ValueError, match="subject must be"):
            await service.rebac_explain(
                subject="not-a-tuple",
                permission="read",
                object=("file", "/doc.txt"),
            )

    @pytest.mark.asyncio
    async def test_explain_rejects_invalid_object(self, service):
        """Test that explain rejects invalid object tuples."""
        with pytest.raises(ValueError, match="object must be"):
            await service.rebac_explain(
                subject=("user", "alice"),
                permission="read",
                object="not-a-tuple",
            )

    @pytest.mark.asyncio
    async def test_explain_uses_zone_from_context(self, service, mock_rebac_manager):
        """Test that zone is extracted from context."""
        ctx = OperationContext(
            user="alice", groups=[], zone_id="zone-acme", is_system=False, is_admin=False
        )
        await service.rebac_explain(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
            context=ctx,
        )

        call_kwargs = mock_rebac_manager.rebac_explain.call_args[1]
        assert call_kwargs["zone_id"] == "zone-acme"


# =========================================================================
# rebac_delete Tests
# =========================================================================


class TestReBACDelete:
    """Test rebac_delete - removing relationship tuples."""

    @pytest.mark.asyncio
    async def test_delete_existing_tuple(self, service, mock_rebac_manager):
        """Test deleting an existing tuple returns True."""
        mock_rebac_manager.rebac_delete.return_value = True
        result = await service.rebac_delete(tuple_id="tuple-123")
        assert result is True
        mock_rebac_manager.rebac_delete.assert_called_once_with(tuple_id="tuple-123")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_tuple(self, service, mock_rebac_manager):
        """Test deleting a non-existent tuple returns False."""
        mock_rebac_manager.rebac_delete.return_value = False
        result = await service.rebac_delete(tuple_id="nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_raises_without_manager(self, no_manager_service):
        """Test that delete raises RuntimeError without manager."""
        with pytest.raises(RuntimeError, match="ReBAC manager is not available"):
            await no_manager_service.rebac_delete(tuple_id="tuple-123")


# =========================================================================
# Configuration Tests (set_rebac_option / get_rebac_option)
# =========================================================================


class TestReBACConfig:
    """Test ReBAC configuration management."""

    def test_set_max_depth(self, service, mock_rebac_manager):
        """Test setting max_depth option."""
        service.set_rebac_option("max_depth", 15)
        assert mock_rebac_manager.max_depth == 15

    def test_set_cache_ttl(self, service, mock_rebac_manager):
        """Test setting cache_ttl option."""
        service.set_rebac_option("cache_ttl", 600)
        assert mock_rebac_manager.cache_ttl_seconds == 600

    def test_set_invalid_option_raises(self, service):
        """Test that setting an unknown option raises ValueError."""
        with pytest.raises(ValueError, match="Unknown ReBAC option"):
            service.set_rebac_option("nonexistent", 42)

    def test_set_max_depth_invalid_type(self, service):
        """Test that non-integer max_depth is rejected."""
        with pytest.raises(ValueError, match="max_depth must be a positive integer"):
            service.set_rebac_option("max_depth", "not-an-int")

    def test_set_max_depth_zero_rejected(self, service):
        """Test that zero max_depth is rejected."""
        with pytest.raises(ValueError, match="max_depth must be a positive integer"):
            service.set_rebac_option("max_depth", 0)

    def test_set_cache_ttl_negative_rejected(self, service):
        """Test that negative cache_ttl is rejected."""
        with pytest.raises(ValueError, match="cache_ttl must be a non-negative integer"):
            service.set_rebac_option("cache_ttl", -1)

    def test_get_max_depth(self, service, mock_rebac_manager):
        """Test getting max_depth option."""
        mock_rebac_manager.max_depth = 20
        result = service.get_rebac_option("max_depth")
        assert result == 20

    def test_get_cache_ttl(self, service, mock_rebac_manager):
        """Test getting cache_ttl option."""
        mock_rebac_manager.cache_ttl_seconds = 600
        result = service.get_rebac_option("cache_ttl")
        assert result == 600

    def test_get_invalid_option_raises(self, service):
        """Test that getting an unknown option raises ValueError."""
        with pytest.raises(ValueError, match="Unknown ReBAC option"):
            service.get_rebac_option("nonexistent")

    def test_config_raises_without_manager(self, no_manager_service):
        """Test that config operations raise RuntimeError without manager."""
        with pytest.raises(RuntimeError, match="ReBAC manager is not available"):
            no_manager_service.set_rebac_option("max_depth", 10)
        with pytest.raises(RuntimeError, match="ReBAC manager is not available"):
            no_manager_service.get_rebac_option("max_depth")


# =========================================================================
# register_namespace Tests
# =========================================================================


class TestRegisterNamespace:
    """Test namespace registration for permission models."""

    def test_register_namespace_valid(self, service, mock_rebac_manager):
        """Test registering a valid namespace configuration."""
        namespace = {
            "object_type": "file",
            "config": {
                "relations": {"viewer": {}, "editor": {}},
                "permissions": {"read": ["viewer", "editor"], "write": ["editor"]},
            },
        }

        service.register_namespace(namespace)
        mock_rebac_manager.create_namespace.assert_called_once()

    def test_register_namespace_rejects_non_dict(self, service):
        """Test that non-dict namespace is rejected."""
        with pytest.raises(ValueError, match="namespace must be a dictionary"):
            service.register_namespace("not-a-dict")

    def test_register_namespace_requires_object_type(self, service):
        """Test that namespace without object_type is rejected."""
        with pytest.raises(ValueError, match="must have 'object_type'"):
            service.register_namespace({"config": {}})

    def test_register_namespace_requires_config(self, service):
        """Test that namespace without config is rejected."""
        with pytest.raises(ValueError, match="must have 'config'"):
            service.register_namespace({"object_type": "file"})

    def test_register_namespace_raises_without_manager(self, no_manager_service):
        """Test that namespace registration raises without manager."""
        with pytest.raises(RuntimeError, match="ReBAC manager is not available"):
            no_manager_service.register_namespace(
                {
                    "object_type": "file",
                    "config": {},
                }
            )


# =========================================================================
# _get_subject_from_context Tests
# =========================================================================


class TestGetSubjectFromContext:
    """Test the helper method that extracts subjects from various context formats."""

    def test_extract_from_dict_with_subject_tuple(self, service):
        """Test extracting subject from dict with 'subject' key."""
        ctx = {"subject": ("user", "alice")}
        result = service._get_subject_from_context(ctx)
        assert result == ("user", "alice")

    def test_extract_from_dict_with_subject_type_and_id(self, service):
        """Test extracting subject from dict with type and id keys."""
        ctx = {"subject_type": "user", "subject_id": "bob"}
        result = service._get_subject_from_context(ctx)
        assert result == ("user", "bob")

    def test_extract_from_dict_with_user_key(self, service):
        """Test extracting subject from dict with 'user' key."""
        ctx = {"user": "charlie"}
        result = service._get_subject_from_context(ctx)
        assert result == ("user", "charlie")

    def test_extract_from_operation_context(self, service, operation_context):
        """Test extracting subject from OperationContext."""
        result = service._get_subject_from_context(operation_context)
        assert result is not None
        assert result[1] == "test_user"

    def test_returns_none_for_none_context(self, service):
        """Test that None context returns None."""
        result = service._get_subject_from_context(None)
        assert result is None


# =========================================================================
# _check_share_permission Tests
# =========================================================================


class TestCheckSharePermission:
    """Test the permission guard used before sharing operations."""

    def test_skips_check_when_no_context(self, enforced_service):
        """Test that no context means no permission check (open access)."""
        # Should not raise
        enforced_service._check_share_permission(
            resource=("file", "/doc.txt"),
            context=None,
        )

    def test_skips_check_when_permissions_disabled(self, service):
        """Test that disabled enforcement skips the check."""
        ctx = OperationContext(
            user="user1", groups=[], zone_id="z1", is_system=False, is_admin=False
        )
        # Should not raise even though user may not have permission
        service._check_share_permission(
            resource=("file", "/doc.txt"),
            context=ctx,
        )

    def test_skips_check_for_admin(self, enforced_service):
        """Test that admin context bypasses permission check."""
        admin_ctx = OperationContext(
            user="admin", groups=["admin"], zone_id="z1", is_system=False, is_admin=True
        )
        # Should not raise
        enforced_service._check_share_permission(
            resource=("file", "/doc.txt"),
            context=admin_ctx,
        )

    def test_skips_check_for_system(self, enforced_service):
        """Test that system context bypasses permission check."""
        system_ctx = OperationContext(
            user="system", groups=[], zone_id="z1", is_system=True, is_admin=False
        )
        # Should not raise
        enforced_service._check_share_permission(
            resource=("file", "/doc.txt"),
            context=system_ctx,
        )

    def test_denies_non_owner_for_non_file_resource(self, enforced_service, mock_rebac_manager):
        """Test that non-owners cannot manage non-file resources.

        The permission check calls self._rebac_manager.rebac_check() (sync) directly.
        When rebac_check returns False, PermissionError must be raised.
        """
        mock_rebac_manager.rebac_check.return_value = False

        ctx = OperationContext(
            user="user1", groups=[], zone_id="z1", is_system=False, is_admin=False
        )
        with pytest.raises(PermissionError, match="does not have owner permission"):
            enforced_service._check_share_permission(
                resource=("group", "developers"),
                context=ctx,
            )

    def test_allows_owner_for_non_file_resource(self, enforced_service, mock_rebac_manager):
        """Test that owners can manage non-file resources.

        When rebac_check returns True for ownership, no exception is raised.
        """
        mock_rebac_manager.rebac_check.return_value = True

        ctx = OperationContext(
            user="owner1", groups=[], zone_id="z1", is_system=False, is_admin=False
        )
        # Should not raise - owner has permission
        enforced_service._check_share_permission(
            resource=("group", "developers"),
            context=ctx,
        )
