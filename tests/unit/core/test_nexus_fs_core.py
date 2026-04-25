"""Tests for NexusFS VFS API surface (Issue #1287, Decision 10A).

Tests verify that NexusFS defines the expected public API surface
for core VFS operations. This ensures no methods are accidentally
dropped during refactoring.

Strategy: Test at the class/API level:
1. Method existence and @rpc_expose decorators
2. Method signatures (parameter names match expected API)
3. Async vs sync classification

For integration tests of actual read/write behavior, see tests/integration/.
"""

import inspect

import pytest

from nexus.core.nexus_fs import NexusFS

# ── Expected API Surface ──────────────────────────────────────────────────────
# These are the extraction-critical methods that MUST exist after refactoring.

CORE_READ_METHODS = {
    "sys_read": {"params": ["path", "count", "offset", "context"], "async": False},
    "read_range": {"params": ["path", "start", "end", "context"], "async": False},
    "stream": {"params": ["path", "chunk_size", "context"], "async": False},
}

CORE_WRITE_METHODS = {
    "sys_write": {
        "params": ["path", "buf", "count", "offset", "context"],
        "async": False,
    },
    "write_stream": {"params": ["path", "chunks", "context"], "async": False},
    "append": {"params": ["path", "content", "context", "if_match", "force"], "async": False},
    "edit": {
        "params": ["path", "edits", "context", "if_match", "fuzzy_threshold", "preview"],
        "async": False,
    },
}

CORE_DELETE_METHODS = {
    "sys_unlink": {"params": ["path", "context"], "async": False},
    "sys_rename": {"params": ["old_path", "new_path", "context"], "async": False},
}

CORE_METADATA_METHODS = {
    "stat": {"params": ["path", "context"], "async": False},
    "access": {"params": ["path", "context"], "async": False},
}

ALL_CORE_METHODS = {
    **CORE_READ_METHODS,
    **CORE_WRITE_METHODS,
    **CORE_DELETE_METHODS,
    **CORE_METADATA_METHODS,
}

# Bulk methods on NexusFS (service-layer convenience, future extraction target).
BULK_METHODS = {
    "read_bulk": {"params": ["paths", "context", "return_metadata", "skip_errors"], "async": False},
    "write_batch": {"params": ["files", "context"], "async": False},
    "delete_batch": {"params": ["paths", "recursive", "context"], "async": False},
    "rename_batch": {"params": ["renames", "context"], "async": False},
    "exists_batch": {"params": ["paths", "context"], "async": False},
}


class TestCoreAPIExists:
    """Verify all VFS methods exist on NexusFS."""

    @pytest.mark.parametrize("method_name", list(ALL_CORE_METHODS.keys()))
    def test_method_exists(self, method_name: str):
        """Test that VFS method exists on NexusFS."""
        assert hasattr(NexusFS, method_name), f"NexusFS.{method_name}() missing"

    @pytest.mark.parametrize("method_name", list(ALL_CORE_METHODS.keys()))
    def test_method_is_callable(self, method_name: str):
        """Test that method is callable."""
        method = getattr(NexusFS, method_name)
        assert callable(method), f"{method_name} is not callable"


class TestCoreRPCExposed:
    """Verify all public methods have @rpc_expose decorators."""

    @pytest.mark.parametrize("method_name", list(ALL_CORE_METHODS.keys()))
    def test_has_rpc_expose(self, method_name: str):
        """Test that method has @rpc_expose decorator."""
        method = getattr(NexusFS, method_name)
        assert hasattr(method, "_rpc_exposed"), (
            f"NexusFS.{method_name}() missing @rpc_expose — will not be available via RPC"
        )


class TestCoreSignatures:
    """Verify method signatures match expected parameters."""

    @pytest.mark.parametrize(
        "method_name,expected",
        list(ALL_CORE_METHODS.items()),
    )
    def test_parameter_names(self, method_name: str, expected: dict):
        """Test that method accepts expected parameters."""
        method = getattr(NexusFS, method_name)
        sig = inspect.signature(method)
        param_names = [p for p in sig.parameters if p != "self"]

        for expected_param in expected["params"]:
            assert expected_param in param_names, (
                f"NexusFS.{method_name}() missing parameter '{expected_param}'. Has: {param_names}"
            )


class TestCoreAsyncClassification:
    """Verify sync vs async classification of methods."""

    @pytest.mark.parametrize(
        "method_name,expected",
        list(ALL_CORE_METHODS.items()),
    )
    def test_sync_vs_async(self, method_name: str, expected: dict):
        """Test that method is sync or async as expected."""
        method = getattr(NexusFS, method_name)
        is_async = inspect.iscoroutinefunction(method)
        assert is_async == expected["async"], (
            f"NexusFS.{method_name}() should be "
            f"{'async' if expected['async'] else 'sync'} but is "
            f"{'async' if is_async else 'sync'}"
        )


class TestCoreMethodGroups:
    """Verify method grouping by domain."""

    def test_read_methods_count(self):
        """Verify expected number of read methods."""
        assert len(CORE_READ_METHODS) == 3, "Expected 3 read methods (sys_read, read_range, stream)"

    def test_write_methods_count(self):
        """Verify expected number of write methods."""
        assert len(CORE_WRITE_METHODS) == 4, "Expected 4 write methods"

    def test_delete_methods_count(self):
        """Verify expected number of delete/rename methods."""
        assert len(CORE_DELETE_METHODS) == 2, "Expected 2 delete/rename methods"

    def test_metadata_methods_count(self):
        """Verify expected number of metadata methods."""
        assert len(CORE_METADATA_METHODS) == 2, "Expected 2 metadata methods"

    def test_total_core_methods(self):
        """Verify total count of core VFS methods."""
        assert len(ALL_CORE_METHODS) == 11, "Expected 11 core methods"

    def test_bulk_methods_count(self):
        """Verify total count of bulk methods on NexusFS."""
        assert len(BULK_METHODS) == 5, "Expected 5 bulk methods on NexusFS"


class TestBulkMethodsOnNexusFS:
    """Verify bulk methods exist on NexusFS."""

    @pytest.mark.parametrize("method_name", list(BULK_METHODS.keys()))
    def test_method_exists(self, method_name: str):
        assert hasattr(NexusFS, method_name), f"NexusFS.{method_name}() missing"

    @pytest.mark.parametrize("method_name", list(BULK_METHODS.keys()))
    def test_method_is_callable(self, method_name: str):
        method = getattr(NexusFS, method_name)
        assert callable(method), f"{method_name} is not callable"

    @pytest.mark.parametrize("method_name", list(BULK_METHODS.keys()))
    def test_has_rpc_expose(self, method_name: str):
        method = getattr(NexusFS, method_name)
        assert hasattr(method, "_rpc_exposed"), f"NexusFS.{method_name}() missing @rpc_expose"

    @pytest.mark.parametrize(
        "method_name,expected",
        list(BULK_METHODS.items()),
    )
    def test_parameter_names(self, method_name: str, expected: dict):
        method = getattr(NexusFS, method_name)
        sig = inspect.signature(method)
        param_names = [p for p in sig.parameters if p != "self"]
        for expected_param in expected["params"]:
            assert expected_param in param_names, (
                f"NexusFS.{method_name}() missing parameter '{expected_param}'. Has: {param_names}"
            )
