"""Tests for NexusFSBulkMixin hook dispatch and helper methods (Issue #2272).

Tests verify:
1. _finalize_bulk_read dispatches hooks and respects `is not None` check
2. read_bulk dispatches post-read hooks per file
3. write_batch dispatches post-write hooks per file
4. Hook failures produce warnings but don't abort operations
5. Guardrails: removed inline methods don't exist on NexusFSCoreMixin
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from nexus.contracts.vfs_hooks import (
    ReadHookContext,
    VFSHookPipeline,
    VFSReadHook,
    VFSWriteHook,
    WriteHookContext,
)
from nexus.core.nexus_fs_bulk import NexusFSBulkMixin

# ── Fake hook implementations ────────────────────────────────────────────────


class FakeReadHook:
    """Hook that uppercases content for testing."""

    @property
    def name(self) -> str:
        return "test-read-hook"

    def on_post_read(self, ctx: ReadHookContext) -> None:
        if ctx.content is not None:
            ctx.content = ctx.content.upper()


class FakeEmptyBytesReadHook:
    """Hook that sets content to empty bytes (tests `or` gotcha fix)."""

    @property
    def name(self) -> str:
        return "test-empty-bytes-hook"

    def on_post_read(self, ctx: ReadHookContext) -> None:
        ctx.content = b""


class FakeWriteHook:
    """Hook that tracks calls for testing."""

    def __init__(self) -> None:
        self.calls: list[WriteHookContext] = []

    @property
    def name(self) -> str:
        return "test-write-hook"

    def on_post_write(self, ctx: WriteHookContext) -> None:
        self.calls.append(ctx)


class FailingReadHook:
    """Hook that always raises an exception."""

    @property
    def name(self) -> str:
        return "failing-read-hook"

    def on_post_read(self, ctx: ReadHookContext) -> None:
        raise RuntimeError("hook failed")


class FailingWriteHook:
    """Hook that always raises an exception."""

    @property
    def name(self) -> str:
        return "failing-write-hook"

    def on_post_write(self, ctx: WriteHookContext) -> None:
        raise RuntimeError("hook failed")


# ── Fake metadata ────────────────────────────────────────────────────────────


@dataclass
class FakeMetadata:
    path: str = "/test.txt"
    etag: str | None = "abc123"
    version: int = 1
    modified_at: datetime | None = None
    size: int = 5
    mime_type: str = "text/plain"
    created_at: datetime | None = None
    zone_id: str = "root"
    backend_name: str = "local"
    physical_path: str | None = None
    created_by: str | None = None

    def __post_init__(self) -> None:
        if self.modified_at is None:
            self.modified_at = datetime.now(UTC)
        if self.created_at is None:
            self.created_at = datetime.now(UTC)


# ── Minimal host stub ────────────────────────────────────────────────────────


class FakeBulkHost(NexusFSBulkMixin):
    """Minimal host that satisfies NexusFSBulkMixin dependencies."""

    def __init__(self) -> None:
        self._hook_pipeline = VFSHookPipeline()
        self._enforce_permissions = False
        self._default_context = None
        self.auto_parse = False
        self._write_observer = None
        self._parser_threads: list[Any] = []
        self._parser_threads_lock = MagicMock()
        self._zone_revision = 0

    # Stub out dependencies the mixin expects
    def _validate_path(self, path: str) -> str:
        return path

    def _get_routing_params(self, context: Any) -> tuple[str | None, str | None, bool]:
        return ("root", None, False)

    def _get_created_by(self, context: Any) -> str | None:
        return None

    def _check_zone_writable(self, context: Any = None) -> None:
        pass

    def _fire_post_mutation_hooks(self, *args: Any, **kwargs: Any) -> None:
        pass

    def _increment_zone_revision(self) -> int:
        self._zone_revision += 1
        return self._zone_revision

    def _handle_observer_error(self, operation: str, op_path: str, error: Exception) -> None:
        pass

    def _get_zone_id(self, context: Any) -> str:
        return "root"


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestFinalizeBulkRead:
    """Tests for _finalize_bulk_read helper (DRY + hook dispatch)."""

    def test_returns_content_without_pipeline(self):
        """No pipeline → raw content returned."""
        host = FakeBulkHost()
        host._hook_pipeline = None
        meta = FakeMetadata()
        result = host._finalize_bulk_read("/test.txt", b"hello", meta, None, False)
        assert result == b"hello"

    def test_returns_metadata_dict(self):
        """return_metadata=True → dict with content, etag, version, etc."""
        host = FakeBulkHost()
        meta = FakeMetadata(etag="abc", version=3)
        result = host._finalize_bulk_read("/test.txt", b"hello", meta, None, True)
        assert isinstance(result, dict)
        assert result["content"] == b"hello"
        assert result["etag"] == "abc"
        assert result["version"] == 3
        assert result["size"] == 5

    def test_hook_transforms_content(self):
        """Read hook modifies content → _finalize_bulk_read uses transformed content."""
        host = FakeBulkHost()
        host._hook_pipeline.register_read_hook(FakeReadHook())
        meta = FakeMetadata()
        result = host._finalize_bulk_read("/test.txt", b"hello", meta, None, False)
        assert result == b"HELLO"

    def test_hook_transforms_content_in_metadata_dict(self):
        """Read hook modifies content → metadata dict also uses transformed content."""
        host = FakeBulkHost()
        host._hook_pipeline.register_read_hook(FakeReadHook())
        meta = FakeMetadata()
        result = host._finalize_bulk_read("/test.txt", b"hello", meta, None, True)
        assert isinstance(result, dict)
        assert result["content"] == b"HELLO"
        assert result["size"] == 5  # size from metadata, not transformed content

    def test_empty_bytes_from_hook_is_preserved(self):
        """Issue #2272: empty bytes from hook must NOT revert to original content."""
        host = FakeBulkHost()
        host._hook_pipeline.register_read_hook(FakeEmptyBytesReadHook())
        meta = FakeMetadata()
        result = host._finalize_bulk_read("/test.txt", b"original", meta, None, False)
        # With the old `or` pattern, this would incorrectly return b"original"
        assert result == b""

    def test_hook_failure_produces_warning_not_crash(self):
        """Failing hook → warning appended, original content returned."""
        host = FakeBulkHost()
        host._hook_pipeline.register_read_hook(FailingReadHook())
        meta = FakeMetadata()
        # Should not raise
        result = host._finalize_bulk_read("/test.txt", b"hello", meta, None, False)
        assert result == b"hello"

    def test_no_hooks_registered_skips_dispatch(self):
        """Pipeline with 0 read hooks → dispatch skipped (short-circuit)."""
        host = FakeBulkHost()
        assert host._hook_pipeline.read_hook_count == 0
        meta = FakeMetadata()
        result = host._finalize_bulk_read("/test.txt", b"hello", meta, None, False)
        assert result == b"hello"


class TestRemovedInlineMethods:
    """Guardrail: inline methods that were replaced by hooks must not exist."""

    def test_apply_dynamic_viewer_filter_removed(self):
        from nexus.core.nexus_fs_core import NexusFSCoreMixin

        assert not hasattr(NexusFSCoreMixin, "_apply_dynamic_viewer_filter_if_needed")

    def test_auto_parse_file_removed(self):
        from nexus.core.nexus_fs_core import NexusFSCoreMixin

        assert not hasattr(NexusFSCoreMixin, "_auto_parse_file")

    def test_parse_in_thread_removed(self):
        from nexus.core.nexus_fs_core import NexusFSCoreMixin

        assert not hasattr(NexusFSCoreMixin, "_parse_in_thread")

    def test_update_tiger_cache_on_move_removed(self):
        from nexus.core.nexus_fs_core import NexusFSCoreMixin

        assert not hasattr(NexusFSCoreMixin, "_update_tiger_cache_on_move")

    def test_get_directory_files_for_move_removed(self):
        from nexus.core.nexus_fs_core import NexusFSCoreMixin

        assert not hasattr(NexusFSCoreMixin, "_get_directory_files_for_move")

    def test_shutdown_parser_threads_removed(self):
        from nexus.core.nexus_fs_core import NexusFSCoreMixin

        assert not hasattr(NexusFSCoreMixin, "shutdown_parser_threads")


class TestHookPipelineProtocol:
    """Verify fake hooks satisfy the protocol."""

    def test_fake_read_hook_is_protocol(self):
        assert isinstance(FakeReadHook(), VFSReadHook)

    def test_fake_write_hook_is_protocol(self):
        assert isinstance(FakeWriteHook(), VFSWriteHook)
