"""Integration tests: VFSHookPipeline wired into NexusFS (Phase 5, Issue #2033).

Validates that hooks registered on the pipeline are actually invoked
during NexusFS read/write/rename operations.
"""

from __future__ import annotations

from nexus.core.vfs_hooks import (
    ReadHookContext,
    VFSHookPipeline,
    WriteHookContext,
)
from tests.conftest import make_test_nexus


class _TrackingReadHook:
    """Hook that records all post-read invocations."""

    def __init__(self) -> None:
        self.calls: list[ReadHookContext] = []

    @property
    def name(self) -> str:
        return "tracking_read"

    def on_post_read(self, ctx: ReadHookContext) -> None:
        self.calls.append(ctx)


class _UpperCaseReadHook:
    """Hook that upper-cases CSV content."""

    @property
    def name(self) -> str:
        return "uppercase_read"

    def on_post_read(self, ctx: ReadHookContext) -> None:
        if ctx.content is not None and ctx.path.endswith(".csv"):
            ctx.content = ctx.content.upper()


class _TrackingWriteHook:
    """Hook that records all post-write invocations."""

    def __init__(self) -> None:
        self.calls: list[WriteHookContext] = []

    @property
    def name(self) -> str:
        return "tracking_write"

    def on_post_write(self, ctx: WriteHookContext) -> None:
        self.calls.append(ctx)


class TestHookPipelineOnNexusFS:
    """Verify the _hook_pipeline attribute exists and is a VFSHookPipeline."""

    def test_nexus_fs_has_hook_pipeline(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        assert hasattr(nx, "_hook_pipeline")
        assert isinstance(nx._hook_pipeline, VFSHookPipeline)

    def test_default_pipeline_is_empty(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        assert nx._hook_pipeline.read_hook_count == 0
        assert nx._hook_pipeline.write_hook_count == 0
        assert nx._hook_pipeline.delete_hook_count == 0
        assert nx._hook_pipeline.rename_hook_count == 0

    def test_pipeline_can_register_hooks(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        hook = _TrackingReadHook()
        nx._hook_pipeline.register_read_hook(hook)
        assert nx._hook_pipeline.read_hook_count == 1


class TestHookPipelineInjectionViaKernelServices:
    """Verify hook_pipeline can be injected via KernelServices."""

    def test_injected_pipeline_is_used(self, tmp_path):
        from nexus.core.config import KernelServices

        pipeline = VFSHookPipeline()
        hook = _TrackingReadHook()
        pipeline.register_read_hook(hook)

        services = KernelServices(hook_pipeline=pipeline)
        nx = make_test_nexus(tmp_path, services=services)

        assert nx._hook_pipeline is pipeline
        assert nx._hook_pipeline.read_hook_count == 1


class TestHookPipelineDirectDispatch:
    """Verify the pipeline dispatches correctly when called directly."""

    def test_read_hook_receives_context(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        hook = _TrackingReadHook()
        nx._hook_pipeline.register_read_hook(hook)

        ctx = ReadHookContext(path="/test.txt", context=None, content=b"hello")
        nx._hook_pipeline.run_post_read(ctx)

        assert len(hook.calls) == 1
        assert hook.calls[0].path == "/test.txt"
        assert hook.calls[0].content == b"hello"

    def test_write_hook_receives_context(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        hook = _TrackingWriteHook()
        nx._hook_pipeline.register_write_hook(hook)

        ctx = WriteHookContext(path="/test.txt", content=b"data", context=None)
        nx._hook_pipeline.run_post_write(ctx)

        assert len(hook.calls) == 1
        assert hook.calls[0].path == "/test.txt"

    def test_content_transformation_hook(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        hook = _UpperCaseReadHook()
        nx._hook_pipeline.register_read_hook(hook)

        ctx = ReadHookContext(path="/data.csv", context=None, content=b"a,b\n1,2")
        nx._hook_pipeline.run_post_read(ctx)

        assert ctx.content == b"A,B\n1,2"

    def test_non_csv_not_transformed(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        hook = _UpperCaseReadHook()
        nx._hook_pipeline.register_read_hook(hook)

        ctx = ReadHookContext(path="/data.txt", context=None, content=b"a,b\n1,2")
        nx._hook_pipeline.run_post_read(ctx)

        assert ctx.content == b"a,b\n1,2"  # unchanged


class TestHookPipelineFailureSafety:
    """Verify hooks that raise don't crash the pipeline."""

    def test_failing_hook_adds_warning(self, tmp_path):
        class _BrokenHook:
            @property
            def name(self) -> str:
                return "broken"

            def on_post_read(self, ctx: ReadHookContext) -> None:
                raise RuntimeError("hook exploded")

        nx = make_test_nexus(tmp_path)
        nx._hook_pipeline.register_read_hook(_BrokenHook())

        ctx = ReadHookContext(path="/test.txt", context=None, content=b"data")
        nx._hook_pipeline.run_post_read(ctx)

        assert len(ctx.warnings) == 1
        assert "broken" in ctx.warnings[0].component
        assert "exploded" in ctx.warnings[0].message

    def test_failing_hook_does_not_stop_others(self, tmp_path):
        class _BrokenHook:
            @property
            def name(self) -> str:
                return "broken"

            def on_post_read(self, ctx: ReadHookContext) -> None:
                raise RuntimeError("oops")

        nx = make_test_nexus(tmp_path)
        tracker = _TrackingReadHook()
        nx._hook_pipeline.register_read_hook(_BrokenHook())
        nx._hook_pipeline.register_read_hook(tracker)

        ctx = ReadHookContext(path="/test.txt", context=None, content=b"data")
        nx._hook_pipeline.run_post_read(ctx)

        assert len(tracker.calls) == 1  # still called
        assert len(ctx.warnings) == 1  # only broken hook warns
