"""Integration tests: VFSHookPipeline wired into NexusFS (Phase 5, Issue #2033).

Validates that hooks registered on the pipeline are actually invoked
during NexusFS read/write/rename operations.
"""

from __future__ import annotations

from nexus.core.vfs_hooks import (
    DeleteHookContext,
    ReadHookContext,
    RenameHookContext,
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


class _TrackingDeleteHook:
    """Hook that records all post-delete invocations."""

    def __init__(self) -> None:
        self.calls: list[DeleteHookContext] = []

    @property
    def name(self) -> str:
        return "tracking_delete"

    def on_post_delete(self, ctx: DeleteHookContext) -> None:
        self.calls.append(ctx)


class _TrackingRenameHook:
    """Hook that records all post-rename invocations."""

    def __init__(self) -> None:
        self.calls: list[RenameHookContext] = []

    @property
    def name(self) -> str:
        return "tracking_rename"

    def on_post_rename(self, ctx: RenameHookContext) -> None:
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


class TestHookPipelineInjectionViaSystemServices:
    """Verify hook_pipeline can be injected via SystemServices."""

    def test_injected_pipeline_is_used(self, tmp_path):
        from nexus.core.config import SystemServices

        pipeline = VFSHookPipeline()
        hook = _TrackingReadHook()
        pipeline.register_read_hook(hook)

        sys_svc = SystemServices(hook_pipeline=pipeline)
        nx = make_test_nexus(tmp_path, system_services=sys_svc)

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


class TestHookPipelineE2EDispatch:
    """Verify hooks are invoked during REAL NexusFS read/write/delete/rename ops.

    These tests register tracking hooks, perform actual VFS operations, and
    confirm the pipeline dispatched to each hook with the correct context.
    """

    def test_read_dispatches_post_read_hook(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        tracker = _TrackingReadHook()
        nx._hook_pipeline.register_read_hook(tracker)

        # Write a file first, then read it
        nx.write("/test.txt", b"hello world")
        content = nx.read("/test.txt")

        assert content == b"hello world"
        assert len(tracker.calls) == 1
        assert tracker.calls[0].path == "/test.txt"
        assert tracker.calls[0].content == b"hello world"

    def test_read_hook_can_transform_content(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        hook = _UpperCaseReadHook()
        nx._hook_pipeline.register_read_hook(hook)

        nx.write("/data.csv", b"name,age\nalice,30")
        content = nx.read("/data.csv")

        assert content == b"NAME,AGE\nALICE,30"

    def test_read_hook_skips_non_csv(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        hook = _UpperCaseReadHook()
        nx._hook_pipeline.register_read_hook(hook)

        nx.write("/data.txt", b"name,age\nalice,30")
        content = nx.read("/data.txt")

        assert content == b"name,age\nalice,30"  # unchanged

    def test_write_dispatches_post_write_hook(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        tracker = _TrackingWriteHook()
        nx._hook_pipeline.register_write_hook(tracker)

        nx.write("/test.txt", b"hello")

        assert len(tracker.calls) == 1
        assert tracker.calls[0].path == "/test.txt"
        assert tracker.calls[0].content == b"hello"
        assert tracker.calls[0].is_new_file is True

    def test_write_hook_receives_update_context(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        tracker = _TrackingWriteHook()
        nx._hook_pipeline.register_write_hook(tracker)

        nx.write("/test.txt", b"v1")
        nx.write("/test.txt", b"v2")

        assert len(tracker.calls) == 2
        assert tracker.calls[0].is_new_file is True  # first write = new file
        assert tracker.calls[1].is_new_file is False  # second write = update

    def test_delete_dispatches_post_delete_hook(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        tracker = _TrackingDeleteHook()
        nx._hook_pipeline.register_delete_hook(tracker)

        nx.write("/test.txt", b"data")
        nx.delete("/test.txt")

        assert len(tracker.calls) == 1
        assert tracker.calls[0].path == "/test.txt"
        assert tracker.calls[0].metadata is not None

    def test_rename_dispatches_post_rename_hook(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        tracker = _TrackingRenameHook()
        nx._hook_pipeline.register_rename_hook(tracker)

        nx.write("/old.txt", b"data")
        nx.rename("/old.txt", "/new.txt")

        assert len(tracker.calls) == 1
        assert tracker.calls[0].old_path == "/old.txt"
        assert tracker.calls[0].new_path == "/new.txt"

    def test_multiple_hooks_all_fire(self, tmp_path):
        nx = make_test_nexus(tmp_path)
        read_tracker = _TrackingReadHook()
        write_tracker = _TrackingWriteHook()
        delete_tracker = _TrackingDeleteHook()
        rename_tracker = _TrackingRenameHook()

        nx._hook_pipeline.register_read_hook(read_tracker)
        nx._hook_pipeline.register_write_hook(write_tracker)
        nx._hook_pipeline.register_delete_hook(delete_tracker)
        nx._hook_pipeline.register_rename_hook(rename_tracker)

        nx.write("/a.txt", b"data")  # write hook fires
        nx.read("/a.txt")  # read hook fires
        nx.rename("/a.txt", "/b.txt")  # rename hook fires
        nx.delete("/b.txt")  # delete hook fires

        assert len(write_tracker.calls) == 1
        assert len(read_tracker.calls) == 1
        assert len(rename_tracker.calls) == 1
        assert len(delete_tracker.calls) == 1
