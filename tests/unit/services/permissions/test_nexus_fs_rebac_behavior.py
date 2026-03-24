"""Behavior tests for ReBAC methods.

This test suite focuses on testable behaviors without requiring full database setup.
Tests cover context extraction, permission checks, validation, and delegation patterns.

NOTE (Issue #2033): ReBAC methods were extracted from NexusFS to ReBACService as part
of the LEGO microkernel decomposition. MockNexusFS implements the original behavioral
contracts directly, using self._require_rebac for manager access. Methods that still
exist on NexusFS (rebac_create, rebac_check, etc.) are bound from NexusFS and route
through rebac_service.
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest

pytest.importorskip("pyroaring")


from nexus.bricks.rebac.rebac_service import ReBACService
from nexus.lib.context_utils import get_subject_from_context

# NOTE (Issue #2440): rebac_create, rebac_check, rebac_delete, rebac_list_tuples
# were deleted from NexusFS (Phase 3: kernel surface reduction). MockNexusFS now
# delegates to self.rebac_service sync methods instead of binding from NexusFS.

ROOT_ZONE_ID = "default"


class MockNexusFS:
    """Test double that reproduces the original NexusFS ReBAC API contract.

    Methods still on NexusFS are bound directly. Methods extracted to ReBACService
    are re-implemented here matching the original calling conventions so that tests
    can mock internal calls (e.g. fs.rebac_check, fs.rebac_list_tuples).
    """

    def __init__(self, rebac_manager=None, enforce_permissions=True):
        self._rebac_manager = rebac_manager
        self._enforce_permissions = enforce_permissions
        self._permission_enforcer = MagicMock()
        self._current_zone_id = ROOT_ZONE_ID
        # ReBACService instance for methods bound from NexusFS
        self.rebac_service = ReBACService(
            rebac_manager=rebac_manager,
            enforce_permissions=enforce_permissions,
            permission_enforcer=self._permission_enforcer,
        )

    def _validate_path(self, path):
        return path

    # --- Property: _require_rebac ---
    @property
    def _require_rebac(self):
        mgr = self._rebac_manager
        if mgr is None:
            raise RuntimeError("ReBAC manager not available")
        return mgr

    # --- Delegation to rebac_service (Issue #2440: methods deleted from NexusFS) ---
    def rebac_create(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.rebac_service.rebac_create_sync(*args, **kwargs)

    def rebac_check(self, *args: Any, **kwargs: Any) -> bool:
        return self.rebac_service.rebac_check_sync(*args, **kwargs)

    def rebac_delete(self, tuple_id: str) -> bool:
        return self.rebac_service.rebac_delete_sync(tuple_id)

    def rebac_list_tuples(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.rebac_service.rebac_list_tuples_sync(**kwargs)

    # --- Methods implemented here (extracted from NexusFS to ReBACService) ---

    def _check_share_permission(
        self,
        resource: tuple[str, str],
        context: Any,
        required_permission: str = "execute",
    ) -> None:
        if not context:
            return
        from nexus.contracts.types import OperationContext, Permission

        op_context: OperationContext | None = None
        if isinstance(context, OperationContext):
            op_context = context
        elif isinstance(context, dict):
            op_context = OperationContext(
                user_id=context.get("user_id", "unknown"),
                groups=context.get("groups", []),
                zone_id=context.get("zone_id"),
                is_admin=context.get("is_admin", False),
                is_system=context.get("is_system", False),
            )
        if not op_context or not self._enforce_permissions:
            return
        if op_context.is_admin or op_context.is_system:
            return

        permission_map = {
            "execute": Permission.EXECUTE,
            "write": Permission.WRITE,
            "read": Permission.READ,
        }
        perm_enum = permission_map.get(required_permission, Permission.EXECUTE)

        if resource[0] == "file":
            resource_path = resource[1]
        else:
            has_permission = self.rebac_check(
                subject=get_subject_from_context(context) or ("user", op_context.user_id),
                permission="owner",
                object=resource,
                context=context,
            )
            if not has_permission:
                raise PermissionError(
                    f"Access denied: User '{op_context.user_id}' does not have owner "
                    f"permission to manage {resource[0]} '{resource[1]}'"
                )
            return

        if hasattr(self, "_permission_enforcer"):
            has_permission = self._permission_enforcer.check(resource_path, perm_enum, op_context)
            if not has_permission:
                zone_id = None
                if resource_path.startswith("/zone/"):
                    parts = resource_path[6:].split("/", 1)
                    if parts:
                        zone_id = parts[0]
                if not zone_id and hasattr(op_context, "zone_id"):
                    zone_id = op_context.zone_id
                if zone_id and op_context.user_id:
                    from nexus.lib.zone_helpers import is_zone_admin

                    if is_zone_admin(self._rebac_manager, op_context.user_id, zone_id):
                        return
                perm_name = required_permission.upper()
                raise PermissionError(
                    f"Access denied: User '{op_context.user_id}' does not have {perm_name} "
                    f"permission to manage permissions on '{resource_path}'. "
                    f"Only owners or zone admins can share resources."
                )

    def rebac_expand(self, permission: str, object: tuple[str, str]) -> list[tuple[str, str]]:
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")
        return self._require_rebac.rebac_expand(permission=permission, object=object)

    def rebac_explain(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        context: Any = None,
    ) -> dict:
        if not isinstance(subject, tuple) or len(subject) != 2:
            raise ValueError(f"subject must be (type, id) tuple, got {subject}")
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")
        effective_zone_id = zone_id
        if effective_zone_id is None and context:
            if isinstance(context, dict):
                effective_zone_id = context.get("zone")
            elif hasattr(context, "zone_id"):
                effective_zone_id = context.zone_id
        return self._require_rebac.rebac_explain(
            subject=subject, permission=permission, object=object, zone_id=effective_zone_id
        )

    def rebac_check_batch(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> list[bool]:
        mgr = self._require_rebac
        for i, check in enumerate(checks):
            if not isinstance(check, tuple) or len(check) != 3:
                raise ValueError(f"Check {i} must be (subject, permission, object) tuple")
            subj, _perm, obj = check
            if not isinstance(subj, tuple) or len(subj) != 2:
                raise ValueError(f"Check {i}: subject must be (type, id) tuple, got {subj}")
            if not isinstance(obj, tuple) or len(obj) != 2:
                raise ValueError(f"Check {i}: object must be (type, id) tuple, got {obj}")
        return mgr.rebac_check_batch_fast(checks=checks)

    def get_rebac_option(self, key: str) -> Any:
        mgr = self._require_rebac
        if key == "max_depth":
            return mgr.max_depth
        elif key == "cache_ttl":
            return mgr.cache_ttl_seconds
        else:
            raise ValueError(f"Unknown ReBAC option: {key}. Valid options: max_depth, cache_ttl")

    def get_namespace(self, object_type: str) -> dict[str, Any] | None:
        ns = self._require_rebac.get_namespace(object_type)
        if ns is None:
            return None
        return {
            "namespace_id": ns.namespace_id,
            "object_type": ns.object_type,
            "config": ns.config,
            "created_at": ns.created_at.isoformat(),
            "updated_at": ns.updated_at.isoformat(),
        }

    def share_with_user(
        self,
        resource: tuple[str, str],
        user_id: str,
        relation: str = "viewer",
        zone_id: str | None = None,
        user_zone_id: str | None = None,
        expires_at: Any = None,
        context: Any = None,
    ) -> dict[str, Any]:
        self._check_share_permission(resource=resource, context=context)
        relation_map = {
            "viewer": "shared-viewer",
            "editor": "shared-editor",
            "owner": "shared-owner",
        }
        if relation not in relation_map:
            raise ValueError(f"relation must be 'viewer', 'editor', or 'owner', got '{relation}'")
        tuple_relation = relation_map[relation]
        expires_dt = None
        if expires_at is not None:
            if isinstance(expires_at, str):
                from datetime import datetime as dt

                expires_dt = dt.fromisoformat(expires_at.replace("Z", "+00:00"))
            else:
                expires_dt = expires_at
        result = self._require_rebac.rebac_write(
            subject=("user", user_id),
            relation=tuple_relation,
            object=resource,
            zone_id=zone_id,
            subject_zone_id=user_zone_id,
            expires_at=expires_dt,
        )
        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
            "consistency_token": result.consistency_token,
        }

    def share_with_group(
        self,
        resource: tuple[str, str],
        group_id: str,
        relation: str = "viewer",
        zone_id: str | None = None,
        group_zone_id: str | None = None,
        expires_at: Any = None,
        context: Any = None,
    ) -> dict[str, Any]:
        self._check_share_permission(resource=resource, context=context)
        relation_map = {
            "viewer": "shared-viewer",
            "editor": "shared-editor",
            "owner": "shared-owner",
        }
        if relation not in relation_map:
            raise ValueError(f"relation must be 'viewer', 'editor', or 'owner', got '{relation}'")
        tuple_relation = relation_map[relation]
        expires_dt = None
        if expires_at is not None:
            if isinstance(expires_at, str):
                from datetime import datetime as dt

                expires_dt = dt.fromisoformat(expires_at.replace("Z", "+00:00"))
            else:
                expires_dt = expires_at
        result = self._require_rebac.rebac_write(
            subject=("group", group_id, "member"),
            relation=tuple_relation,
            object=resource,
            zone_id=zone_id,
            subject_zone_id=group_zone_id,
            expires_at=expires_dt,
        )
        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
            "consistency_token": result.consistency_token,
        }

    def list_outgoing_shares(
        self,
        resource: tuple[str, str] | None = None,
        zone_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        from nexus.bricks.rebac.cache.iterator import CursorExpiredError

        relation_to_level = {
            "shared-viewer": "viewer",
            "shared-editor": "editor",
            "shared-owner": "owner",
        }

        def _transform(tuples):
            return [
                {
                    "share_id": t.get("tuple_id"),
                    "resource_type": t.get("object_type"),
                    "resource_id": t.get("object_id"),
                    "recipient_id": t.get("subject_id"),
                    "permission_level": relation_to_level.get(t.get("relation") or "", "viewer"),
                    "created_at": t.get("created_at"),
                    "expires_at": t.get("expires_at"),
                }
                for t in tuples
            ]

        def _compute():
            all_tuples = self.rebac_list_tuples(
                relation_in=["shared-viewer", "shared-editor", "shared-owner"],
                object=resource,
            )
            return _transform(all_tuples)

        current_zone = zone_id or self._current_zone_id
        if cursor:
            try:
                items, next_cursor, total = self._require_rebac._iterator_cache.get_page(
                    cursor_id=cursor,
                    offset=offset,
                    limit=limit,
                )
                return {
                    "items": items,
                    "next_cursor": next_cursor,
                    "total_count": total,
                    "has_more": next_cursor is not None,
                }
            except CursorExpiredError:
                pass

        resource_str = f"{resource[0]}:{resource[1]}" if resource else "all"
        query_hash = f"outgoing:{current_zone}:{resource_str}"
        cursor_id, all_results, total = self._require_rebac._iterator_cache.get_or_create(
            query_hash=query_hash,
            zone_id=current_zone,
            compute_fn=_compute,
        )
        items = all_results[offset : offset + limit]
        has_more = offset + limit < total
        next_cursor_val = cursor_id if has_more else None
        return {
            "items": items,
            "next_cursor": next_cursor_val,
            "total_count": total,
            "has_more": has_more,
        }

    def list_incoming_shares(
        self,
        user_id: str,
        limit: int = 100,
        offset: int = 0,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        from nexus.bricks.rebac.cache.iterator import CursorExpiredError

        relation_to_level = {
            "shared-viewer": "viewer",
            "shared-editor": "editor",
            "shared-owner": "owner",
        }

        def _transform(tuples):
            return [
                {
                    "share_id": t.get("tuple_id"),
                    "resource_type": t.get("object_type"),
                    "resource_id": t.get("object_id"),
                    "owner_zone_id": t.get("zone_id"),
                    "permission_level": relation_to_level.get(t.get("relation") or "", "viewer"),
                    "created_at": t.get("created_at"),
                    "expires_at": t.get("expires_at"),
                }
                for t in tuples
            ]

        def _compute():
            all_tuples = self.rebac_list_tuples(
                subject=("user", user_id),
                relation_in=["shared-viewer", "shared-editor", "shared-owner"],
            )
            return _transform(all_tuples)

        current_zone = self._current_zone_id
        if cursor:
            try:
                items, next_cursor, total = self._require_rebac._iterator_cache.get_page(
                    cursor_id=cursor,
                    offset=offset,
                    limit=limit,
                )
                return {
                    "items": items,
                    "next_cursor": next_cursor,
                    "total_count": total,
                    "has_more": next_cursor is not None,
                }
            except CursorExpiredError:
                pass

        query_hash = f"incoming:{current_zone}:{user_id}"
        cursor_id, all_results, total = self._require_rebac._iterator_cache.get_or_create(
            query_hash=query_hash,
            zone_id=current_zone,
            compute_fn=_compute,
        )
        items = all_results[offset : offset + limit]
        has_more = offset + limit < total
        next_cursor_val = cursor_id if has_more else None
        return {
            "items": items,
            "next_cursor": next_cursor_val,
            "total_count": total,
            "has_more": has_more,
        }

    def get_dynamic_viewer_config(
        self,
        subject: tuple[str, str],
        file_path: str,
    ) -> dict[str, Any] | None:
        tuples = self.rebac_list_tuples(
            subject=subject, relation="dynamic_viewer", object=("file", file_path)
        )
        if not tuples:
            return None
        tuple_data = tuples[0]
        conditions = self._require_rebac.get_tuple_conditions(tuple_data["tuple_id"])
        if conditions and conditions.get("type") == "dynamic_viewer":
            return conditions.get("column_config")
        return None


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
    """Tests for get_subject_from_context (nexus.lib.context_utils)."""

    def test_dict_with_subject_key(self):
        """Extracts subject from dict with 'subject' key."""
        context = {"subject": ("user", "alice")}
        assert get_subject_from_context(context) == ("user", "alice")

    def test_dict_with_subject_type_and_id(self):
        """Constructs subject from dict with 'subject_type' and 'subject_id' keys."""
        context = {"subject_type": "agent", "subject_id": "bob"}
        assert get_subject_from_context(context) == ("agent", "bob")

    def test_dict_with_user_id_key(self):
        """Extracts from 'user_id' key in dict when subject fields are missing."""
        context = {"user_id": "charlie"}
        assert get_subject_from_context(context) == ("user", "charlie")

    def test_dict_with_subject_type_without_id_uses_user_id(self):
        """Uses 'user_id' field as subject_id when subject_id is missing."""
        context = {"subject_type": "agent", "user_id": "dave"}
        assert get_subject_from_context(context) == ("agent", "dave")

    def test_operation_context_with_get_subject_method(self):
        """Extracts subject using get_subject() method from context object."""
        mock_context = Mock()
        mock_context.get_subject = Mock(return_value=("group", "developers"))
        assert get_subject_from_context(mock_context) == ("group", "developers")
        mock_context.get_subject.assert_called_once()

    def test_operation_context_get_subject_returns_none(self):
        """Returns None when get_subject() method returns None."""
        mock_context = Mock()
        mock_context.get_subject = Mock(return_value=None)
        assert get_subject_from_context(mock_context) is None

    def test_object_with_subject_type_and_id_attributes(self):
        """Extracts subject from object with subject_type and subject_id attributes."""
        mock_context = Mock(spec=[])
        mock_context.subject_type = "workspace"
        mock_context.subject_id = "project1"
        assert get_subject_from_context(mock_context) == ("workspace", "project1")

    def test_object_with_user_attribute(self):
        """Extracts subject from object with only user attribute."""
        mock_context = Mock(spec=["user"])
        mock_context.user_id = "eve"
        assert get_subject_from_context(mock_context) == ("user", "eve")

    def test_none_context_returns_none(self):
        """Returns None when context is None."""
        assert get_subject_from_context(None) is None

    def test_empty_dict_returns_none(self):
        """Returns None when context is empty dict."""
        assert get_subject_from_context({}) is None


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
        from nexus.contracts.types import OperationContext

        fs = MockNexusFS()
        resource = ("file", "/test/doc.txt")
        # Pass OperationContext directly
        context = OperationContext(
            user_id="admin",
            groups=[],
            is_admin=True,
        )

        # Should not raise
        fs._check_share_permission(resource, context)

    def test_system_context_bypasses_check(self):
        """Bypasses permission check for system context."""
        from nexus.contracts.types import OperationContext

        fs = MockNexusFS()
        resource = ("file", "/test/doc.txt")
        # Pass OperationContext directly
        context = OperationContext(
            user_id="system",
            groups=[],
            is_system=True,
        )

        # Should not raise
        fs._check_share_permission(resource, context)

    def test_non_file_resource_checks_rebac_owner(self):
        """Checks ReBAC 'owner' permission for non-file resources."""
        from nexus.contracts.types import OperationContext

        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs.rebac_check = Mock(return_value=True)

        resource = ("workspace", "project1")
        context = OperationContext(
            user_id="alice",
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
        from nexus.contracts.types import OperationContext

        mock_manager = MagicMock()
        fs = MockNexusFS(rebac_manager=mock_manager)
        fs.rebac_check = Mock(return_value=False)

        resource = ("group", "developers")
        context = OperationContext(
            user_id="bob",
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
        fs.rebac_service._check_share_permission = Mock()

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
        fs.rebac_service._check_share_permission = Mock()

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
        fs.rebac_service._check_share_permission = Mock()

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
        fs.rebac_service._check_share_permission = Mock()

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
        fs.rebac_service._check_share_permission = Mock()

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
        fs.rebac_service._check_share_permission = Mock()

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
        fs.rebac_service._check_share_permission = Mock()

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
        mock_manager = MagicMock()

        # Mock the conditions with column_config
        column_config = {
            "hidden_columns": ["password"],
            "aggregations": {"age": "mean"},
            "visible_columns": ["name", "email"],
        }

        mock_manager.get_tuple_conditions = Mock(
            return_value={
                "type": "dynamic_viewer",
                "column_config": column_config,
            }
        )

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
