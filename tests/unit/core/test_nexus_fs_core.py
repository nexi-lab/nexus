"""Tests for NexusFSCoreMixin extraction-critical methods (Issue #1287, Decision 10A).

Tests verify that NexusFSCoreMixin defines the expected public API surface
that will be extracted into VFSCore. This ensures no methods are accidentally
dropped during brick extraction.

Strategy: Since NexusFSCoreMixin is a mixin that requires full NexusFS
(which needs Rust binary), we test at the class/API level:
1. Method existence and @rpc_expose decorators
2. Method signatures (parameter names match expected API)
3. Async vs sync classification

For integration tests of actual read/write behavior, see tests/integration/.
"""

from __future__ import annotations

import inspect

import pytest

from nexus.core.nexus_fs_core import NexusFSCoreMixin

# ── Expected API Surface ──────────────────────────────────────────────────────
# These are the extraction-critical methods that MUST exist after refactoring.

CORE_READ_METHODS = {
    "read": {"params": ["path", "context", "return_metadata", "parsed"], "async": False},
    "read_bulk": {"params": ["paths", "context", "return_metadata", "skip_errors"], "async": False},
    "read_range": {"params": ["path", "start", "end", "context"], "async": False},
    "stream": {"params": ["path", "chunk_size", "context"], "async": False},
}

CORE_WRITE_METHODS = {
    "write": {
        "params": ["path", "content", "context", "if_match", "if_none_match", "force"],
        "async": False,
    },
    "write_batch": {"params": ["files", "context"], "async": False},
    "write_stream": {"params": ["path", "chunks", "context"], "async": False},
    "append": {"params": ["path", "content", "context", "if_match", "force"], "async": False},
    "edit": {
        "params": ["path", "edits", "context", "if_match", "fuzzy_threshold", "preview"],
        "async": False,
    },
}

CORE_DELETE_METHODS = {
    "delete": {"params": ["path", "context"], "async": False},
    "delete_bulk": {"params": ["paths", "recursive", "context"], "async": False},
    "rename": {"params": ["old_path", "new_path", "context"], "async": False},
    "rename_bulk": {"params": ["renames", "context"], "async": False},
}

CORE_METADATA_METHODS = {
    "stat": {"params": ["path", "context"], "async": False},
    "stat_bulk": {"params": ["paths", "context", "skip_errors"], "async": False},
    "exists": {"params": ["path", "context"], "async": False},
    "exists_batch": {"params": ["paths", "context"], "async": False},
    "metadata_batch": {"params": ["paths", "context"], "async": False},
}

ALL_CORE_METHODS = {
    **CORE_READ_METHODS,
    **CORE_WRITE_METHODS,
    **CORE_DELETE_METHODS,
    **CORE_METADATA_METHODS,
}


class TestCoreMixinAPIExists:
    """Verify all extraction-critical methods exist on the mixin."""

    @pytest.mark.parametrize("method_name", list(ALL_CORE_METHODS.keys()))
    def test_method_exists(self, method_name: str):
        """Test that extraction-critical method exists on mixin."""
        assert hasattr(NexusFSCoreMixin, method_name), (
            f"NexusFSCoreMixin.{method_name}() missing — "
            f"extraction to VFSCore would lose this method"
        )

    @pytest.mark.parametrize("method_name", list(ALL_CORE_METHODS.keys()))
    def test_method_is_callable(self, method_name: str):
        """Test that method is callable."""
        method = getattr(NexusFSCoreMixin, method_name)
        assert callable(method), f"{method_name} is not callable"


class TestCoreMixinRPCExposed:
    """Verify all public methods have @rpc_expose decorators."""

    @pytest.mark.parametrize("method_name", list(ALL_CORE_METHODS.keys()))
    def test_has_rpc_expose(self, method_name: str):
        """Test that method has @rpc_expose decorator."""
        method = getattr(NexusFSCoreMixin, method_name)
        assert hasattr(method, "_rpc_exposed"), (
            f"NexusFSCoreMixin.{method_name}() missing @rpc_expose — "
            f"will not be available via RPC after extraction"
        )


class TestCoreMixinSignatures:
    """Verify method signatures match expected parameters."""

    @pytest.mark.parametrize(
        "method_name,expected",
        list(ALL_CORE_METHODS.items()),
    )
    def test_parameter_names(self, method_name: str, expected: dict):
        """Test that method accepts expected parameters."""
        method = getattr(NexusFSCoreMixin, method_name)
        sig = inspect.signature(method)
        param_names = [p for p in sig.parameters if p != "self"]

        for expected_param in expected["params"]:
            assert expected_param in param_names, (
                f"NexusFSCoreMixin.{method_name}() missing parameter '{expected_param}'. "
                f"Has: {param_names}"
            )


class TestCoreMixinAsyncClassification:
    """Verify sync vs async classification of methods."""

    @pytest.mark.parametrize(
        "method_name,expected",
        list(ALL_CORE_METHODS.items()),
    )
    def test_sync_vs_async(self, method_name: str, expected: dict):
        """Test that method is sync or async as expected."""
        method = getattr(NexusFSCoreMixin, method_name)
        is_async = inspect.iscoroutinefunction(method)
        assert is_async == expected["async"], (
            f"NexusFSCoreMixin.{method_name}() should be "
            f"{'async' if expected['async'] else 'sync'} but is "
            f"{'async' if is_async else 'sync'}"
        )


class TestCoreMixinMethodGroups:
    """Verify method grouping by domain for extraction guidance."""

    def test_read_methods_count(self):
        """Verify expected number of read methods."""
        assert len(CORE_READ_METHODS) == 4, (
            "Expected 4 read methods (read, read_bulk, read_range, stream)"
        )

    def test_write_methods_count(self):
        """Verify expected number of write methods."""
        assert len(CORE_WRITE_METHODS) == 5, "Expected 5 write methods"

    def test_delete_methods_count(self):
        """Verify expected number of delete/rename methods."""
        assert len(CORE_DELETE_METHODS) == 4, "Expected 4 delete/rename methods"

    def test_metadata_methods_count(self):
        """Verify expected number of metadata methods."""
        assert len(CORE_METADATA_METHODS) == 5, "Expected 5 metadata methods"

    def test_total_extraction_critical_methods(self):
        """Verify total count of extraction-critical methods."""
        assert len(ALL_CORE_METHODS) == 18, "Expected 18 extraction-critical methods"
