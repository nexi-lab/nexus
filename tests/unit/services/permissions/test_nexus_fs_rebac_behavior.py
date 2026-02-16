"""Behavior tests for NexusFSReBACMixin class.

This test suite focuses on testable behaviors without requiring full database setup.
Tests cover context extraction, permission checks, validation, and delegation patterns.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, Mock

import pytest

from nexus.core.nexus_fs import NexusFS


class MockNexusFS:
    """Test fixture class that provides mock attributes for ReBAC methods on NexusFS.

    Uses NexusFS methods directly by binding them to this mock class.
    """

    def __init__(self, rebac_manager=None, enforce_permissions=True):
        self._rebac_manager = rebac_manager
        self._enforce_permissions = enforce_permissions
        self._permission_enforcer = MagicMock()

    def _validate_path(self, path):
        """Mock path validation that returns path unchanged."""
        return path

    # Bind ReBAC methods from NexusFS to this mock class
    _require_rebac = NexusFS._require_rebac  # type: ignore[assignment]
    _get_subject_from_context = NexusFS._get_subject_from_context
    _check_share_permission = NexusFS._check_share_permission
    rebac_create = NexusFS.rebac_create
    rebac_check = NexusFS.rebac_check
    rebac_expand = NexusFS.rebac_expand
    rebac_explain = NexusFS.rebac_explain
    rebac_check_batch = NexusFS.rebac_check_batch
    rebac_delete = NexusFS.rebac_delete
    rebac_list_tuples = NexusFS.rebac_list_tuples
    get_namespace = NexusFS.get_namespace
    set_rebac_option = NexusFS.set_rebac_option
    get_rebac_option = NexusFS.get_rebac_option
    register_namespace = NexusFS.register_namespace
    namespace_create = NexusFS.namespace_create
    namespace_list = NexusFS.namespace_list
    namespace_delete = NexusFS.namespace_delete
    rebac_expand_with_privacy = NexusFS.rebac_expand_with_privacy
    grant_consent = NexusFS.grant_consent
    revoke_consent = NexusFS.revoke_consent
    make_public = NexusFS.make_public
    make_private = NexusFS.make_private
    share_with_user = NexusFS.share_with_user
    share_with_group = NexusFS.share_with_group
    revoke_share = NexusFS.revoke_share
    revoke_share_by_id = NexusFS.revoke_share_by_id
    list_outgoing_shares = NexusFS.list_outgoing_shares
    list_incoming_shares = NexusFS.list_incoming_shares
    get_dynamic_viewer_config = NexusFS.get_dynamic_viewer_config
    apply_dynamic_viewer_filter = NexusFS.apply_dynamic_viewer_filter
    read_with_dynamic_viewer = NexusFS.read_with_dynamic_viewer
    grant_traverse_on_implicit_dirs = NexusFS.grant_traverse_on_implicit_dirs
    process_tiger_cache_queue = NexusFS.process_tiger_cache_queue
    warm_tiger_cache = NexusFS.warm_tiger_cache


class TestRequireRebac:
    """Tests for _require_rebac property."""

    def test_returns_manager_when_available(self):
        """Returns rebac_manager when it is set."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)

        result = fs._require_rebac

        assert result is mock_manager

    def test_raises_runtime_error_when_none(self):
        """Raises RuntimeError when rebac_manager is None."""
        fs = MockNexusFS(rebac_manager=None)

        with pytest.raises(RuntimeError, match="ReBAC manager not available"):
            _ = fs._require_rebac


class TestGetSubjectFromContext:
    """Tests for _get_subject_from_context method."""

    def test_dict_with_subject_key(self):
        """Extracts subject from dict with 'subject' key."""
        fs = MockNexusFS()
        context = {"subject": ("user", "alice")}

        result = fs._get_subject_from_context(context)

        assert result == ("user", "alice")

    def test_dict_with_subject_type_and_id(self):
        """Constructs subject from dict with 'subject_type' and 'subject_id' keys."""
        fs = MockNexusFS()
        context = {"subject_type": "agent", "subject_id": "bob"}

        result = fs._get_subject_from_context(context)

        assert result == ("agent", "bob")

    def test_dict_with_user_key_fallback(self):
        """Falls back to 'user' key in dict when subject fields are missing."""
        fs = MockNexusFS()
        context = {"user": "charlie"}

        result = fs._get_subject_from_context(context)

        assert result == ("user", "charlie")

    def test_dict_with_subject_type_without_id_uses_user(self):
        """Uses 'user' field as subject_id when subject_id is missing."""
        fs = MockNexusFS()
        context = {"subject_type": "agent", "user": "dave"}

        result = fs._get_subject_from_context(context)

        assert result == ("agent", "dave")

    def test_operation_context_with_get_subject_method(self):
        """Extracts subject using get_subject() method from context object."""
        fs = MockNexusFS()
        mock_context = Mock()
        mock_context.get_subject = Mock(return_value=("group", "developers"))

        result = fs._get_subject_from_context(mock_context)

        assert result == ("group", "developers")
        mock_context.get_subject.assert_called_once()

    def test_operation_context_get_subject_returns_none(self):
        """Returns None when get_subject() method returns None."""
        fs = MockNexusFS()
        mock_context = Mock()
        mock_context.get_subject = Mock(return_value=None)

        result = fs._get_subject_from_context(mock_context)

        assert result is None

    def test_object_with_subject_type_and_id_attributes(self):
        """Extracts subject from object with subject_type and subject_id attributes."""
        fs = MockNexusFS()
        mock_context = Mock(spec=[])
        mock_context.subject_type = "workspace"
        mock_context.subject_id = "project1"

        result = fs._get_subject_from_context(mock_context)

        assert result == ("workspace", "project1")

    def test_object_with_user_attribute(self):
        """Extracts subject from object with only user attribute."""
        fs = MockNexusFS()
        mock_context = Mock(spec=["user"])
        mock_context.user = "eve"

        result = fs._get_subject_from_context(mock_context)

        assert result == ("user", "eve")

    def test_none_context_returns_none(self):
        """Returns None when context is None."""
        fs = MockNexusFS()

        result = fs._get_subject_from_context(None)

        assert result is None

    def test_empty_dict_returns_none(self):
        """Returns None when context is empty dict."""
        fs = MockNexusFS()
        context = {}

        result = fs._get_subject_from_context(context)

        assert result is None


class TestCheckSharePermission:
    """Tests for _check_share_permission method."""

    def test_none_context_no_check(self):
        """Skips permission check when context is None."""
        fs = MockNexusFS()
        resource = ("file", "/test/doc.txt")

        # Should not raise
        fs._check_share_permission(resource, None)

    def test_admin_context_bypasses_check(self):
        """Bypasses permission check for admin context."""
        from nexus.core.permissions import OperationContext

        fs = MockNexusFS()
        resource = ("file", "/test/doc.txt")
        # Pass OperationContext directly
        context = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
        )

        # Should not raise
        fs._check_share_permission(resource, context)

    def test_system_context_bypasses_check(self):
        """Bypasses permission check for system context."""
        from nexus.core.permissions import OperationContext

        fs = MockNexusFS()
        resource = ("file", "/test/doc.txt")
        # Pass OperationContext directly
        context = OperationContext(
            user="system",
            groups=[],
            is_system=True,
        )

        # Should not raise
        fs._check_share_permission(resource, context)

    def test_non_file_resource_checks_rebac_owner(self):
        """Checks ReBAC 'owner' permission for non-file resources."""
        from nexus.core.permissions import OperationContext

        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs.rebac_check = Mock(return_value=True)

        resource = ("workspace", "project1")
        context = OperationContext(
            user="alice",
            groups=[],
        )

        # Should not raise when has owner permission
        fs._check_share_permission(resource, context)

        # Verify rebac_check was called with owner permission
        fs.rebac_check.assert_called_once()
        call_args = fs.rebac_check.call_args
        assert call_args[1]["permission"] == "owner"
        assert call_args[1]["object"] == resource

    def test_non_file_resource_raises_when_not_owner(self):
        """Raises PermissionError when user is not owner of non-file resource."""
        from nexus.core.permissions import OperationContext

        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs.rebac_check = Mock(return_value=False)

        resource = ("group", "developers")
        context = OperationContext(
            user="bob",
            groups=[],
        )

        with pytest.raises(PermissionError, match="does not have owner permission"):
            fs._check_share_permission(resource, context)

    def test_skip_when_enforce_permissions_false(self):
        """Skips permission check when _enforce_permissions is False."""
        fs = MockNexusFS(enforce_permissions=False)
        resource = ("workspace", "project1")
        context = {"user": "alice"}

        # Should not raise even without proper permissions
        fs._check_share_permission(resource, context)


class TestRebacCreate:
    """Tests for rebac_create validation logic."""

    def test_invalid_subject_tuple_raises_value_error(self):
        """Raises ValueError when subject tuple is invalid."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)

        with pytest.raises(ValueError, match="subject must be"):
            fs.rebac_create(
                subject=("user",),  # Only 1 element
                relation="viewer",
                object=("file", "/test.txt"),
            )

    def test_invalid_object_tuple_raises_value_error(self):
        """Raises ValueError when object tuple is invalid."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)

        with pytest.raises(ValueError, match="object must be"):
            fs.rebac_create(
                subject=("user", "alice"),
                relation="viewer",
                object=("file",),  # Only 1 element
            )

    def test_dynamic_viewer_on_non_csv_raises_value_error(self):
        """Raises ValueError when dynamic_viewer is used on non-CSV file."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs._check_share_permission = Mock()

        with pytest.raises(ValueError, match="dynamic_viewer relation only supports CSV files"):
            fs.rebac_create(
                subject=("user", "alice"),
                relation="dynamic_viewer",
                object=("file", "/test.txt"),  # Not .csv
                column_config={"hidden_columns": []},
            )

    def test_dynamic_viewer_without_column_config_raises_value_error(self):
        """Raises ValueError when dynamic_viewer is used without column_config."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs._check_share_permission = Mock()

        with pytest.raises(ValueError, match="column_config is required"):
            fs.rebac_create(
                subject=("user", "alice"),
                relation="dynamic_viewer",
                object=("file", "/test.csv"),
                column_config=None,
            )

    def test_column_config_with_non_dynamic_viewer_raises_value_error(self):
        """Raises ValueError when column_config is provided for non-dynamic_viewer relation."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs._check_share_permission = Mock()

        with pytest.raises(
            ValueError, match="can only be provided when relation is 'dynamic_viewer'"
        ):
            fs.rebac_create(
                subject=("user", "alice"),
                relation="viewer",
                object=("file", "/test.csv"),
                column_config={"hidden_columns": []},
            )

    def test_column_in_multiple_categories_raises_value_error(self):
        """Raises ValueError when column appears in multiple categories."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs._check_share_permission = Mock()

        with pytest.raises(ValueError, match="appears in multiple categories"):
            fs.rebac_create(
                subject=("user", "alice"),
                relation="dynamic_viewer",
                object=("file", "/test.csv"),
                column_config={
                    "hidden_columns": ["age"],
                    "aggregations": {"age": "mean"},  # 'age' appears twice
                },
            )

    def test_invalid_aggregation_operation_raises_value_error(self):
        """Raises ValueError when aggregation operation is invalid."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs._check_share_permission = Mock()

        with pytest.raises(ValueError, match="Invalid aggregation operation"):
            fs.rebac_create(
                subject=("user", "alice"),
                relation="dynamic_viewer",
                object=("file", "/test.csv"),
                column_config={
                    "aggregations": {"salary": "average"},  # Invalid operation
                },
            )

    def test_trailing_slash_on_file_path_is_stripped(self):
        """Strips trailing slash from file path."""
        mock_manager = MagicMock()
        mock_manager.rebac_write = Mock(
            return_value=Mock(tuple_id="abc", revision="1", consistency_token="token")
        )
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs._check_share_permission = Mock()

        fs.rebac_create(
            subject=("user", "alice"),
            relation="viewer",
            object=("file", "/test/path/"),  # Trailing slash
        )

        # Verify trailing slash was stripped
        call_args = mock_manager.rebac_write.call_args
        assert call_args[1]["object"] == ("file", "/test/path")

    def test_successful_creation_returns_dict_with_tuple_id(self):
        """Returns dict with tuple_id, revision, and consistency_token on success."""
        mock_manager = MagicMock()
        mock_manager.rebac_write = Mock(
            return_value=Mock(
                tuple_id="test-uuid-123",
                revision="rev-1",
                consistency_token="token-abc",
            )
        )
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs._check_share_permission = Mock()

        result = fs.rebac_create(
            subject=("user", "alice"),
            relation="viewer",
            object=("file", "/test.txt"),
        )

        assert result == {
            "tuple_id": "test-uuid-123",
            "revision": "rev-1",
            "consistency_token": "token-abc",
        }


class TestRebacCheck:
    """Tests for rebac_check method."""

    def test_delegates_to_require_rebac_rebac_check(self):
        """Delegates to _require_rebac.rebac_check with correct parameters."""
        mock_manager = MagicMock()
        mock_manager.rebac_check = Mock(return_value=True)
        fs = MockNexusFS(rebac_manager=mock_manager)

        result = fs.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/test.txt"),
            zone_id="zone1",
        )

        assert result is True
        mock_manager.rebac_check.assert_called_once()
        call_args = mock_manager.rebac_check.call_args
        assert call_args[1]["subject"] == ("user", "alice")
        assert call_args[1]["permission"] == "read"
        assert call_args[1]["object"] == ("file", "/test.txt")
        assert call_args[1]["zone_id"] == "zone1"

    def test_uses_zone_id_from_context_when_not_provided(self):
        """Uses zone_id from context when not explicitly provided."""
        mock_manager = MagicMock()
        mock_manager.rebac_check = Mock(return_value=True)
        fs = MockNexusFS(rebac_manager=mock_manager)

        context = {"zone": "context-zone"}
        fs.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/test.txt"),
            context=context,
        )

        call_args = mock_manager.rebac_check.call_args
        assert call_args[1]["zone_id"] == "context-zone"

    def test_validates_subject_tuple(self):
        """Validates subject tuple format."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)

        with pytest.raises(ValueError, match="subject must be"):
            fs.rebac_check(
                subject="invalid",  # Not a tuple
                permission="read",
                object=("file", "/test.txt"),
            )

    def test_validates_object_tuple(self):
        """Validates object tuple format."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)

        with pytest.raises(ValueError, match="object must be"):
            fs.rebac_check(
                subject=("user", "alice"),
                permission="read",
                object="invalid",  # Not a tuple
            )


class TestRebacDelete:
    """Tests for rebac_delete method."""

    def test_delegates_to_require_rebac_rebac_delete(self):
        """Delegates to _require_rebac.rebac_delete with tuple_id."""
        mock_manager = MagicMock()
        mock_manager.rebac_delete = Mock(return_value=True)
        fs = MockNexusFS(rebac_manager=mock_manager)

        result = fs.rebac_delete("tuple-id-123")

        assert result is True
        mock_manager.rebac_delete.assert_called_once_with(tuple_id="tuple-id-123")

    def test_raises_runtime_error_without_rebac_manager(self):
        """Raises RuntimeError when rebac_manager is not available."""
        fs = MockNexusFS(rebac_manager=None)

        # The property _require_rebac will raise when _rebac_manager is None
        with pytest.raises(RuntimeError, match="ReBAC manager not available"):
            fs.rebac_delete("tuple-id-123")


class TestRebacExpand:
    """Tests for rebac_expand method."""

    def test_delegates_to_require_rebac_rebac_expand(self):
        """Delegates to _require_rebac.rebac_expand with correct parameters."""
        mock_manager = MagicMock()
        mock_manager.rebac_expand = Mock(return_value=[("user", "alice"), ("user", "bob")])
        fs = MockNexusFS(rebac_manager=mock_manager)

        result = fs.rebac_expand(
            permission="read",
            object=("file", "/test.txt"),
        )

        assert result == [("user", "alice"), ("user", "bob")]
        mock_manager.rebac_expand.assert_called_once_with(
            permission="read",
            object=("file", "/test.txt"),
        )

    def test_validates_object_tuple(self):
        """Validates object tuple format."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)

        with pytest.raises(ValueError, match="object must be"):
            fs.rebac_expand(
                permission="read",
                object=["file", "/test.txt"],  # List instead of tuple
            )


class TestRebacExplain:
    """Tests for rebac_explain method."""

    def test_delegates_to_require_rebac_rebac_explain(self):
        """Delegates to _require_rebac.rebac_explain with correct parameters."""
        mock_manager = MagicMock()
        mock_explanation = {
            "result": True,
            "reason": "Direct access",
            "paths": [],
        }
        mock_manager.rebac_explain = Mock(return_value=mock_explanation)
        fs = MockNexusFS(rebac_manager=mock_manager)

        result = fs.rebac_explain(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/test.txt"),
            zone_id="zone1",
        )

        assert result == mock_explanation
        mock_manager.rebac_explain.assert_called_once()
        call_args = mock_manager.rebac_explain.call_args
        assert call_args[1]["subject"] == ("user", "alice")
        assert call_args[1]["permission"] == "read"
        assert call_args[1]["object"] == ("file", "/test.txt")
        assert call_args[1]["zone_id"] == "zone1"

    def test_uses_zone_id_from_dict_context(self):
        """Extracts zone_id from dict context when not provided."""
        mock_manager = MagicMock()
        mock_manager.rebac_explain = Mock(return_value={"result": True})
        fs = MockNexusFS(rebac_manager=mock_manager)

        context = {"zone": "context-zone"}
        fs.rebac_explain(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/test.txt"),
            context=context,
        )

        call_args = mock_manager.rebac_explain.call_args
        assert call_args[1]["zone_id"] == "context-zone"

    def test_validates_subject_and_object_tuples(self):
        """Validates both subject and object tuple formats."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)

        # Invalid subject
        with pytest.raises(ValueError, match="subject must be"):
            fs.rebac_explain(
                subject=("user",),  # Only 1 element
                permission="read",
                object=("file", "/test.txt"),
            )

        # Invalid object
        with pytest.raises(ValueError, match="object must be"):
            fs.rebac_explain(
                subject=("user", "alice"),
                permission="read",
                object=("file",),  # Only 1 element
            )


class TestRebacCheckBatch:
    """Tests for rebac_check_batch method."""

    def test_delegates_to_require_rebac_rebac_check_batch_fast(self):
        """Delegates to _require_rebac.rebac_check_batch_fast."""
        mock_manager = MagicMock()
        mock_manager.rebac_check_batch_fast = Mock(return_value=[True, False, True])
        fs = MockNexusFS(rebac_manager=mock_manager)

        checks = [
            (("user", "alice"), "read", ("file", "/doc1.txt")),
            (("user", "alice"), "write", ("file", "/doc2.txt")),
            (("user", "bob"), "read", ("file", "/doc3.txt")),
        ]

        result = fs.rebac_check_batch(checks)

        assert result == [True, False, True]
        mock_manager.rebac_check_batch_fast.assert_called_once_with(checks=checks)

    def test_validates_check_tuple_format(self):
        """Validates that each check is a 3-element tuple."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)

        invalid_checks = [
            (("user", "alice"), "read"),  # Only 2 elements
        ]

        with pytest.raises(ValueError, match="must be \\(subject, permission, object\\) tuple"):
            fs.rebac_check_batch(invalid_checks)

    def test_validates_subject_in_checks(self):
        """Validates subject format in each check."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)

        invalid_checks = [
            ("alice", "read", ("file", "/test.txt")),  # Subject is not a tuple
        ]

        with pytest.raises(ValueError, match="subject must be"):
            fs.rebac_check_batch(invalid_checks)

    def test_validates_object_in_checks(self):
        """Validates object format in each check."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)

        invalid_checks = [
            (("user", "alice"), "read", "/test.txt"),  # Object is not a tuple
        ]

        with pytest.raises(ValueError, match="object must be"):
            fs.rebac_check_batch(invalid_checks)


class TestGetRebacOption:
    """Tests for get_rebac_option method."""

    def test_get_max_depth_option(self):
        """Gets max_depth configuration option."""
        mock_manager = MagicMock()
        mock_manager.max_depth = 15
        fs = MockNexusFS(rebac_manager=mock_manager)

        result = fs.get_rebac_option("max_depth")

        assert result == 15

    def test_get_cache_ttl_option(self):
        """Gets cache_ttl configuration option."""
        mock_manager = MagicMock()
        mock_manager.cache_ttl_seconds = 600
        fs = MockNexusFS(rebac_manager=mock_manager)

        result = fs.get_rebac_option("cache_ttl")

        assert result == 600

    def test_invalid_option_raises_value_error(self):
        """Raises ValueError for unknown configuration option."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)

        with pytest.raises(ValueError, match="Unknown ReBAC option"):
            fs.get_rebac_option("invalid_option")

    def test_raises_runtime_error_without_rebac_manager(self):
        """Raises RuntimeError when rebac_manager is not available."""
        fs = MockNexusFS(rebac_manager=None)

        with pytest.raises(RuntimeError, match="ReBAC manager not available"):
            fs.get_rebac_option("max_depth")


class TestGetNamespace:
    """Tests for get_namespace method."""

    def test_returns_namespace_dict_when_found(self):
        """Returns namespace configuration dict when namespace exists."""
        mock_manager = MagicMock()
        mock_ns = Mock()
        mock_ns.namespace_id = "ns-123"
        mock_ns.object_type = "file"
        mock_ns.config = {"relations": {}, "permissions": {}}
        mock_ns.created_at = datetime(2024, 1, 1, tzinfo=UTC)
        mock_ns.updated_at = datetime(2024, 1, 2, tzinfo=UTC)
        mock_manager.get_namespace = Mock(return_value=mock_ns)
        fs = MockNexusFS(rebac_manager=mock_manager)

        result = fs.get_namespace("file")

        assert result is not None
        assert result["namespace_id"] == "ns-123"
        assert result["object_type"] == "file"
        assert result["config"] == {"relations": {}, "permissions": {}}
        assert result["created_at"] == "2024-01-01T00:00:00+00:00"
        assert result["updated_at"] == "2024-01-02T00:00:00+00:00"

    def test_returns_none_when_namespace_not_found(self):
        """Returns None when namespace does not exist."""
        mock_manager = MagicMock()
        mock_manager.get_namespace = Mock(return_value=None)
        fs = MockNexusFS(rebac_manager=mock_manager)

        result = fs.get_namespace("nonexistent")

        assert result is None

    def test_raises_runtime_error_without_rebac_manager(self):
        """Raises RuntimeError when rebac_manager is not available."""
        fs = MockNexusFS(rebac_manager=None)

        with pytest.raises(RuntimeError, match="ReBAC manager not available"):
            fs.get_namespace("file")


class TestShareWithUser:
    """Tests for share_with_user method."""

    def test_validates_relation_parameter(self):
        """Raises ValueError when relation is not viewer, editor, or owner."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs._check_share_permission = Mock()

        with pytest.raises(ValueError, match="relation must be"):
            fs.share_with_user(
                resource=("file", "/test.txt"),
                user_id="alice",
                relation="invalid",
            )

    def test_maps_viewer_to_shared_viewer(self):
        """Maps 'viewer' relation to 'shared-viewer' tuple relation."""
        mock_manager = MagicMock()
        mock_manager.rebac_write = Mock(
            return_value=Mock(
                tuple_id="uuid",
                revision="rev",
                consistency_token="token",
            )
        )
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs._check_share_permission = Mock()

        fs.share_with_user(
            resource=("file", "/test.txt"),
            user_id="alice",
            relation="viewer",
        )

        call_args = mock_manager.rebac_write.call_args
        assert call_args[1]["relation"] == "shared-viewer"

    def test_calls_check_share_permission(self):
        """Calls _check_share_permission to verify ownership."""
        mock_manager = MagicMock()
        mock_manager.rebac_write = Mock(
            return_value=Mock(
                tuple_id="uuid",
                revision="rev",
                consistency_token="token",
            )
        )
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs._check_share_permission = Mock()

        context = {"user": "admin"}
        fs.share_with_user(
            resource=("file", "/test.txt"),
            user_id="alice",
            relation="viewer",
            context=context,
        )

        fs._check_share_permission.assert_called_once_with(
            resource=("file", "/test.txt"),
            context=context,
        )


class TestShareWithGroup:
    """Tests for share_with_group method."""

    def test_uses_userset_as_subject_pattern(self):
        """Uses userset-as-subject pattern: (group, group_id, member)."""
        mock_manager = MagicMock()
        mock_manager.rebac_write = Mock(
            return_value=Mock(
                tuple_id="uuid",
                revision="rev",
                consistency_token="token",
            )
        )
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs._check_share_permission = Mock()

        fs.share_with_group(
            resource=("file", "/test.txt"),
            group_id="developers",
            relation="viewer",
        )

        call_args = mock_manager.rebac_write.call_args
        assert call_args[1]["subject"] == ("group", "developers", "member")

    def test_validates_relation_parameter(self):
        """Raises ValueError when relation is not viewer, editor, or owner."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs._check_share_permission = Mock()

        with pytest.raises(ValueError, match="relation must be"):
            fs.share_with_group(
                resource=("file", "/test.txt"),
                group_id="developers",
                relation="custom",
            )

    def test_calls_check_share_permission(self):
        """Calls _check_share_permission to verify ownership."""
        mock_manager = MagicMock()
        mock_manager.rebac_write = Mock(
            return_value=Mock(
                tuple_id="uuid",
                revision="rev",
                consistency_token="token",
            )
        )
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs._check_share_permission = Mock()

        context = {"user": "admin"}
        fs.share_with_group(
            resource=("file", "/test.txt"),
            group_id="developers",
            relation="editor",
            context=context,
        )

        fs._check_share_permission.assert_called_once_with(
            resource=("file", "/test.txt"),
            context=context,
        )


class TestListOutgoingShares:
    """Tests for list_outgoing_shares method."""

    def test_filters_by_shared_relations(self):
        """Filters tuples by shared-viewer, shared-editor, shared-owner relations."""
        mock_manager = MagicMock()

        # Create a mock that will trigger the compute_fn callback
        def mock_get_or_create(query_hash, zone_id, compute_fn):
            # Call compute_fn to trigger rebac_list_tuples
            compute_fn()
            return ("cursor-id", [], 0)

        mock_manager._iterator_cache = MagicMock()
        mock_manager._iterator_cache.get_or_create = Mock(side_effect=mock_get_or_create)

        fs = MockNexusFS(rebac_manager=mock_manager)
        fs.rebac_list_tuples = Mock(return_value=[])

        fs.list_outgoing_shares()

        # Verify rebac_list_tuples was called with correct relation_in filter
        fs.rebac_list_tuples.assert_called_once()
        call_args = fs.rebac_list_tuples.call_args
        assert call_args[1]["relation_in"] == ["shared-viewer", "shared-editor", "shared-owner"]

    def test_transforms_tuples_to_share_info_format(self):
        """Transforms raw tuples to share info format with permission_level."""
        mock_manager = MagicMock()

        # Transformed shares defined inline (used by compute_fn below)
        _ = [
            {
                "share_id": "uuid-1",
                "resource_type": "file",
                "resource_id": "/test.txt",
                "recipient_id": "alice",
                "permission_level": "viewer",
                "created_at": "2024-01-01",
                "expires_at": None,
            }
        ]

        def mock_get_or_create(query_hash, zone_id, compute_fn):
            # Call compute_fn to get the transformed data
            return ("cursor-id", compute_fn(), 1)

        mock_manager._iterator_cache = MagicMock()
        mock_manager._iterator_cache.get_or_create = Mock(side_effect=mock_get_or_create)

        fs = MockNexusFS(rebac_manager=mock_manager)
        fs.rebac_list_tuples = Mock(
            return_value=[
                {
                    "tuple_id": "uuid-1",
                    "object_type": "file",
                    "object_id": "/test.txt",
                    "subject_id": "alice",
                    "relation": "shared-viewer",
                    "created_at": "2024-01-01",
                    "expires_at": None,
                }
            ]
        )

        result = fs.list_outgoing_shares()

        assert len(result["items"]) == 1
        share = result["items"][0]
        assert share["share_id"] == "uuid-1"
        assert share["resource_type"] == "file"
        assert share["resource_id"] == "/test.txt"
        assert share["recipient_id"] == "alice"
        assert share["permission_level"] == "viewer"

    def test_returns_paginated_response(self):
        """Returns dict with items, next_cursor, total_count, and has_more."""
        mock_manager = MagicMock()
        mock_manager._iterator_cache = MagicMock()
        # Simulate 150 results total, returning first 100
        all_items = [{"share_id": f"uuid-{i}"} for i in range(150)]
        mock_manager._iterator_cache.get_or_create = Mock(
            return_value=("cursor-id", all_items, 150)
        )
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs.rebac_list_tuples = Mock(return_value=[])

        result = fs.list_outgoing_shares(limit=100, offset=0)

        assert "items" in result
        assert "next_cursor" in result
        assert "total_count" in result
        assert "has_more" in result
        assert result["total_count"] == 150
        assert result["has_more"] is True
        assert len(result["items"]) == 100


class TestListIncomingShares:
    """Tests for list_incoming_shares method."""

    def test_filters_by_user_id(self):
        """Filters tuples by subject=(user, user_id)."""
        mock_manager = MagicMock()

        def mock_get_or_create(query_hash, zone_id, compute_fn):
            # Call compute_fn to trigger rebac_list_tuples
            compute_fn()
            return ("cursor-id", [], 0)

        mock_manager._iterator_cache = MagicMock()
        mock_manager._iterator_cache.get_or_create = Mock(side_effect=mock_get_or_create)

        fs = MockNexusFS(rebac_manager=mock_manager)
        fs.rebac_list_tuples = Mock(return_value=[])

        fs.list_incoming_shares(user_id="alice")

        # Verify rebac_list_tuples was called with correct subject filter
        fs.rebac_list_tuples.assert_called_once()
        call_args = fs.rebac_list_tuples.call_args
        assert call_args[1]["subject"] == ("user", "alice")

    def test_transforms_tuples_with_owner_zone_id(self):
        """Transforms tuples to include owner_zone_id."""
        mock_manager = MagicMock()

        def mock_get_or_create(query_hash, zone_id, compute_fn):
            # Call compute_fn to get the transformed data
            return ("cursor-id", compute_fn(), 1)

        mock_manager._iterator_cache = MagicMock()
        mock_manager._iterator_cache.get_or_create = Mock(side_effect=mock_get_or_create)

        fs = MockNexusFS(rebac_manager=mock_manager)
        fs.rebac_list_tuples = Mock(
            return_value=[
                {
                    "tuple_id": "uuid-1",
                    "object_type": "file",
                    "object_id": "/shared.txt",
                    "zone_id": "partner-zone",
                    "relation": "shared-editor",
                    "created_at": "2024-01-01",
                    "expires_at": None,
                }
            ]
        )

        result = fs.list_incoming_shares(user_id="alice")

        assert len(result["items"]) == 1
        share = result["items"][0]
        assert share["owner_zone_id"] == "partner-zone"
        assert share["permission_level"] == "editor"

    def test_returns_paginated_response(self):
        """Returns dict with items, next_cursor, total_count, and has_more."""
        mock_manager = MagicMock()
        mock_manager._iterator_cache = MagicMock()
        all_items = [{"share_id": f"uuid-{i}"} for i in range(50)]
        mock_manager._iterator_cache.get_or_create = Mock(return_value=("cursor-id", all_items, 50))
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs.rebac_list_tuples = Mock(return_value=[])

        result = fs.list_incoming_shares(user_id="alice", limit=20, offset=0)

        assert "items" in result
        assert "next_cursor" in result
        assert "total_count" in result
        assert "has_more" in result
        assert result["total_count"] == 50
        assert len(result["items"]) == 20


class TestGetDynamicViewerConfig:
    """Tests for get_dynamic_viewer_config method."""

    def test_returns_none_when_no_tuples_found(self):
        """Returns None when no dynamic_viewer tuples exist."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs.rebac_list_tuples = Mock(return_value=[])

        result = fs.get_dynamic_viewer_config(
            subject=("user", "alice"),
            file_path="/test.csv",
        )

        assert result is None

    def test_returns_column_config_from_conditions(self):
        """Returns column_config from tuple conditions."""
        import json

        mock_manager = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()

        # Mock the conditions with column_config
        column_config = {
            "hidden_columns": ["password"],
            "aggregations": {"age": "mean"},
            "visible_columns": ["name", "email"],
        }
        conditions = json.dumps(
            {
                "type": "dynamic_viewer",
                "column_config": column_config,
            }
        )

        mock_row = {"conditions": conditions}
        mock_cursor.fetchone = Mock(return_value=mock_row)
        mock_manager._create_cursor = Mock(return_value=mock_cursor)
        mock_manager._get_connection = Mock(return_value=mock_conn)
        mock_manager._close_connection = Mock()
        mock_manager._fix_sql_placeholders = Mock(side_effect=lambda x: x)

        fs = MockNexusFS(rebac_manager=mock_manager)
        fs.rebac_list_tuples = Mock(return_value=[{"tuple_id": "uuid-1"}])

        result = fs.get_dynamic_viewer_config(
            subject=("user", "alice"),
            file_path="/test.csv",
        )

        assert result == column_config

    def test_queries_for_dynamic_viewer_relation(self):
        """Queries rebac_list_tuples with dynamic_viewer relation filter."""
        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs.rebac_list_tuples = Mock(return_value=[])

        fs.get_dynamic_viewer_config(
            subject=("agent", "alice"),
            file_path="/data/users.csv",
        )

        # Verify correct parameters
        fs.rebac_list_tuples.assert_called_once_with(
            subject=("agent", "alice"),
            relation="dynamic_viewer",
            object=("file", "/data/users.csv"),
        )
