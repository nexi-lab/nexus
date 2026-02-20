"""Unit tests for VFSHookPipeline and hook protocols."""

from __future__ import annotations

from nexus.core.vfs_hooks import (
    ReadHookContext,
    RenameHookContext,
    VFSHookPipeline,
    WriteHookContext,
)


class _CountingReadHook:
    """Test hook that counts invocations."""

    def __init__(self, name: str = "counting_read") -> None:
        self._name = name
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    def on_post_read(self, ctx: ReadHookContext) -> None:
        self.call_count += 1


class _FilteringReadHook:
    """Test hook that transforms content."""

    @property
    def name(self) -> str:
        return "filtering_read"

    def on_post_read(self, ctx: ReadHookContext) -> None:
        if ctx.content is not None:
            ctx.content = ctx.content.upper()


class _FailingReadHook:
    """Test hook that always raises."""

    @property
    def name(self) -> str:
        return "failing_read"

    def on_post_read(self, ctx: ReadHookContext) -> None:
        raise RuntimeError("hook exploded")


class _CountingWriteHook:
    """Test hook that counts write invocations."""

    def __init__(self) -> None:
        self.call_count = 0
        self.last_ctx: WriteHookContext | None = None

    @property
    def name(self) -> str:
        return "counting_write"

    def on_post_write(self, ctx: WriteHookContext) -> None:
        self.call_count += 1
        self.last_ctx = ctx


class _FailingWriteHook:
    @property
    def name(self) -> str:
        return "failing_write"

    def on_post_write(self, ctx: WriteHookContext) -> None:
        raise ValueError("write hook failed")


class _CountingRenameHook:
    def __init__(self) -> None:
        self.call_count = 0

    @property
    def name(self) -> str:
        return "counting_rename"

    def on_post_rename(self, ctx: RenameHookContext) -> None:
        self.call_count += 1


class TestVFSHookPipelineRegistration:
    def test_empty_pipeline(self):
        p = VFSHookPipeline()
        assert p.read_hook_count == 0
        assert p.write_hook_count == 0
        assert p.delete_hook_count == 0
        assert p.rename_hook_count == 0

    def test_register_read_hook(self):
        p = VFSHookPipeline()
        p.register_read_hook(_CountingReadHook())
        assert p.read_hook_count == 1

    def test_register_multiple_hooks(self):
        p = VFSHookPipeline()
        p.register_read_hook(_CountingReadHook("a"))
        p.register_read_hook(_CountingReadHook("b"))
        assert p.read_hook_count == 2


class TestVFSHookPipelinePostRead:
    def test_post_read_calls_all_hooks(self):
        p = VFSHookPipeline()
        h1 = _CountingReadHook("h1")
        h2 = _CountingReadHook("h2")
        p.register_read_hook(h1)
        p.register_read_hook(h2)

        ctx = ReadHookContext(path="/test.txt", context=None, content=b"hello")
        p.run_post_read(ctx)

        assert h1.call_count == 1
        assert h2.call_count == 1

    def test_post_read_hook_transforms_content(self):
        p = VFSHookPipeline()
        p.register_read_hook(_FilteringReadHook())

        ctx = ReadHookContext(path="/test.txt", context=None, content=b"hello")
        p.run_post_read(ctx)

        assert ctx.content == b"HELLO"

    def test_failing_hook_adds_warning(self):
        p = VFSHookPipeline()
        p.register_read_hook(_FailingReadHook())

        ctx = ReadHookContext(path="/test.txt", context=None)
        p.run_post_read(ctx)

        assert len(ctx.warnings) == 1
        assert ctx.warnings[0].severity == "degraded"
        assert ctx.warnings[0].component == "failing_read"
        assert "hook exploded" in ctx.warnings[0].message

    def test_failing_hook_does_not_stop_subsequent_hooks(self):
        p = VFSHookPipeline()
        counter = _CountingReadHook()
        p.register_read_hook(_FailingReadHook())
        p.register_read_hook(counter)

        ctx = ReadHookContext(path="/test.txt", context=None)
        p.run_post_read(ctx)

        assert counter.call_count == 1  # still called despite earlier failure
        assert len(ctx.warnings) == 1

    def test_empty_pipeline_no_warnings(self):
        p = VFSHookPipeline()
        ctx = ReadHookContext(path="/test.txt", context=None)
        p.run_post_read(ctx)
        assert len(ctx.warnings) == 0


class TestVFSHookPipelinePostWrite:
    def test_post_write_calls_hook(self):
        p = VFSHookPipeline()
        h = _CountingWriteHook()
        p.register_write_hook(h)

        ctx = WriteHookContext(path="/test.txt", content=b"data", context=None)
        p.run_post_write(ctx)

        assert h.call_count == 1
        assert h.last_ctx is ctx

    def test_failing_write_hook_adds_warning(self):
        p = VFSHookPipeline()
        p.register_write_hook(_FailingWriteHook())

        ctx = WriteHookContext(path="/test.txt", content=b"data", context=None)
        p.run_post_write(ctx)

        assert len(ctx.warnings) == 1
        assert ctx.warnings[0].component == "failing_write"

    def test_write_hook_receives_metadata(self):
        p = VFSHookPipeline()
        h = _CountingWriteHook()
        p.register_write_hook(h)

        ctx = WriteHookContext(
            path="/test.txt",
            content=b"data",
            context=None,
            is_new_file=True,
            content_hash="abc123",
            new_version=1,
            zone_id="test-zone",
        )
        p.run_post_write(ctx)

        assert h.last_ctx is not None
        assert h.last_ctx.is_new_file is True
        assert h.last_ctx.content_hash == "abc123"


class TestVFSHookPipelinePostRename:
    def test_post_rename_calls_hook(self):
        p = VFSHookPipeline()
        h = _CountingRenameHook()
        p.register_rename_hook(h)

        ctx = RenameHookContext(old_path="/a.txt", new_path="/b.txt", context=None)
        p.run_post_rename(ctx)

        assert h.call_count == 1
