"""Tests for admin_only enforcement on @rpc_expose methods.

Issue #1457: backfill_directory_index was missing admin auth check.
This tests the systematic admin_only guard added to @rpc_expose decorator
and enforced in _auto_dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from nexus.core.rpc_decorator import rpc_expose

# ============================================================================
# Test 1: Decorator sets _rpc_admin_only flag
# ============================================================================


class TestRpcExposeAdminOnly:
    """Test that @rpc_expose(admin_only=True) sets the flag on the function."""

    def test_admin_only_default_false(self):
        """admin_only defaults to False for backward compatibility."""

        @rpc_expose(description="normal method")
        def normal_method():
            pass

        assert getattr(normal_method, "_rpc_admin_only", None) is False

    def test_admin_only_true_sets_flag(self):
        """admin_only=True sets _rpc_admin_only on the decorated function."""

        @rpc_expose(description="admin method", admin_only=True)
        def admin_method():
            pass

        assert getattr(admin_method, "_rpc_admin_only", None) is True

    def test_admin_only_false_explicit(self):
        """Explicit admin_only=False works the same as default."""

        @rpc_expose(description="explicit non-admin", admin_only=False)
        def non_admin_method():
            pass

        assert getattr(non_admin_method, "_rpc_admin_only", None) is False

    def test_other_attrs_preserved_with_admin_only(self):
        """Other decorator attrs still work when admin_only is set."""

        @rpc_expose(name="custom_name", description="desc", version="2.0", admin_only=True)
        def my_method():
            pass

        assert my_method._rpc_exposed is True
        assert my_method._rpc_name == "custom_name"
        assert my_method._rpc_description == "desc"
        assert my_method._rpc_version == "2.0"
        assert my_method._rpc_admin_only is True


# ============================================================================
# Test 2: _auto_dispatch enforces admin_only
# ============================================================================


@dataclass
class FakeContext:
    """Minimal context for testing."""

    is_admin: bool = False
    user: str = "testuser"
    zone_id: str = "default"


# Real functions with proper signatures for inspect.signature to work
@rpc_expose(description="admin operation", admin_only=True)
async def _fake_admin_method(_context=None):
    return {"result": "admin_ok"}


@rpc_expose(description="normal operation", admin_only=False)
async def _fake_normal_method(_context=None):
    return {"result": "normal_ok"}


class TestDispatchAdminEnforcement:
    """Test that _dispatch_method rejects non-admin callers for admin_only methods.

    The admin guard is enforced in _dispatch_method (not _auto_dispatch) to
    cover both auto-dispatch and manual dispatch paths (Issue #1457).
    """

    @pytest.fixture(autouse=True)
    def _mock_app_state(self):
        """Patch _fastapi_app.state to provide exposed_methods."""
        from nexus.server.fastapi_server import _fastapi_app

        if _fastapi_app is None:
            # Create a minimal mock app for testing
            mock_app = MagicMock()
            with patch("nexus.server.fastapi_server._fastapi_app", mock_app):
                yield mock_app
        else:
            yield _fastapi_app

    @pytest.mark.asyncio
    async def test_admin_caller_allowed(self, _mock_app_state):
        """Admin callers can invoke admin_only methods."""
        from nexus.server.fastapi_server import _dispatch_method

        admin_context = FakeContext(is_admin=True)
        params = type("Params", (), {})()  # Empty params object

        _mock_app_state.state.exposed_methods = {"admin_op": _fake_admin_method}

        result = await _dispatch_method("admin_op", params, admin_context)
        assert result == {"result": "admin_ok"}

    @pytest.mark.asyncio
    async def test_non_admin_caller_rejected(self, _mock_app_state):
        """Non-admin callers get NexusPermissionError for admin_only methods."""
        from nexus.core.exceptions import NexusPermissionError
        from nexus.server.fastapi_server import _dispatch_method

        non_admin_context = FakeContext(is_admin=False)
        params = type("Params", (), {})()

        _mock_app_state.state.exposed_methods = {"admin_op": _fake_admin_method}

        with pytest.raises(NexusPermissionError, match="Admin privileges required"):
            await _dispatch_method("admin_op", params, non_admin_context)

    @pytest.mark.asyncio
    async def test_none_context_rejected(self, _mock_app_state):
        """None context is rejected for admin_only methods."""
        from nexus.core.exceptions import NexusPermissionError
        from nexus.server.fastapi_server import _dispatch_method

        params = type("Params", (), {})()

        _mock_app_state.state.exposed_methods = {"admin_op": _fake_admin_method}

        with pytest.raises(NexusPermissionError, match="Admin privileges required"):
            await _dispatch_method("admin_op", params, None)

    @pytest.mark.asyncio
    async def test_normal_method_not_affected(self, _mock_app_state):
        """Non-admin_only methods work fine for non-admin callers."""
        from nexus.server.fastapi_server import _dispatch_method

        non_admin_context = FakeContext(is_admin=False)
        params = type("Params", (), {})()

        _mock_app_state.state.exposed_methods = {"normal_op": _fake_normal_method}

        result = await _dispatch_method("normal_op", params, non_admin_context)
        assert result == {"result": "normal_ok"}


# ============================================================================
# Test 3: backfill_directory_index is marked admin_only
# ============================================================================


class TestBackfillDirectoryIndexAdminOnly:
    """Test that backfill_directory_index has admin_only=True on its decorator."""

    def test_backfill_directory_index_is_admin_only(self):
        """Verify the actual method on NexusFS has _rpc_admin_only=True."""
        from nexus.core.nexus_fs import NexusFS

        method = getattr(NexusFS, "backfill_directory_index", None)
        assert method is not None, "backfill_directory_index should exist on NexusFS"
        assert getattr(method, "_rpc_admin_only", False) is True, (
            "backfill_directory_index must be marked admin_only=True"
        )

    def test_backfill_directory_index_is_rpc_exposed(self):
        """Verify the method is still RPC-exposed."""
        from nexus.core.nexus_fs import NexusFS

        method = getattr(NexusFS, "backfill_directory_index", None)
        assert method is not None
        assert getattr(method, "_rpc_exposed", False) is True


# ============================================================================
# Test 4: Safety net — admin-like methods must have admin_only=True
# ============================================================================


class TestAdminMethodAnnotationSafety:
    """Ensure methods with admin-like names are always marked admin_only."""

    # Prefixes that indicate admin-only operations
    ADMIN_PREFIXES = ("backfill_",)

    def test_admin_prefixed_methods_are_admin_only(self):
        """Methods with admin-like prefixes must have admin_only=True.

        This test prevents future developers from adding admin-like
        RPC methods without the admin_only guard.
        """
        from nexus.core.nexus_fs import NexusFS

        violations = []
        for name in dir(NexusFS):
            method = getattr(NexusFS, name, None)
            if (
                method
                and getattr(method, "_rpc_exposed", False)
                and any(name.startswith(prefix) for prefix in self.ADMIN_PREFIXES)
                and not getattr(method, "_rpc_admin_only", False)
            ):
                violations.append(name)

        assert not violations, (
            f"RPC methods with admin-like names missing admin_only=True: {violations}"
        )
