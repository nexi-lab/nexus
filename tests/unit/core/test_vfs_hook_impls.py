"""Unit tests for concrete VFS hook implementations (Phase 4, Issue #2033)."""

import threading
import time
from unittest.mock import MagicMock

from nexus.core.vfs_hook_impls import (
    AutoParseWriteHook,
    DynamicViewerReadHook,
    TigerCacheRenameHook,
)
from nexus.core.vfs_hooks import ReadHookContext, RenameHookContext, WriteHookContext

# =========================================================================
# DynamicViewerReadHook
# =========================================================================


class TestDynamicViewerReadHook:
    def _make_hook(
        self,
        subject: str | None = "user:alice",
        config: dict | None = None,
        filtered: str = "col1\na",
    ) -> DynamicViewerReadHook:
        return DynamicViewerReadHook(
            get_subject=lambda ctx: subject,
            get_viewer_config=lambda s, p: config,
            apply_filter=lambda data, cfg, fmt: {"filtered_data": filtered},
        )

    def test_skips_non_csv(self):
        hook = self._make_hook(config={"columns": ["col1"]})
        ctx = ReadHookContext(path="/test.txt", context=None, content=b"hello")
        hook.on_post_read(ctx)
        assert ctx.content == b"hello"  # unchanged

    def test_skips_no_subject(self):
        hook = self._make_hook(subject=None, config={"columns": ["col1"]})
        ctx = ReadHookContext(path="/test.csv", context=None, content=b"col1,col2\na,b")
        hook.on_post_read(ctx)
        assert ctx.content == b"col1,col2\na,b"  # unchanged

    def test_skips_no_config(self):
        hook = self._make_hook(config=None)
        ctx = ReadHookContext(path="/test.csv", context=None, content=b"col1,col2\na,b")
        hook.on_post_read(ctx)
        assert ctx.content == b"col1,col2\na,b"  # unchanged

    def test_applies_filter(self):
        hook = self._make_hook(config={"columns": ["col1"]}, filtered="col1\na")
        ctx = ReadHookContext(path="/data.csv", context=None, content=b"col1,col2\na,b")
        hook.on_post_read(ctx)
        assert ctx.content == b"col1\na"

    def test_skips_none_content(self):
        hook = self._make_hook(config={"columns": ["col1"]})
        ctx = ReadHookContext(path="/data.csv", context=None, content=None)
        hook.on_post_read(ctx)
        assert ctx.content is None

    def test_handles_bytes_filtered_data(self):
        hook = DynamicViewerReadHook(
            get_subject=lambda ctx: "user:alice",
            get_viewer_config=lambda s, p: {"columns": ["col1"]},
            apply_filter=lambda data, cfg, fmt: {"filtered_data": b"raw-bytes"},
        )
        ctx = ReadHookContext(path="/data.CSV", context=None, content=b"original")
        hook.on_post_read(ctx)
        assert ctx.content == b"raw-bytes"

    def test_name(self):
        hook = self._make_hook()
        assert hook.name == "dynamic_viewer"


# =========================================================================
# AutoParseWriteHook
# =========================================================================


class TestAutoParseWriteHook:
    def test_skips_if_no_parser(self):
        def no_parser(path: str):
            raise ValueError("no parser")

        hook = AutoParseWriteHook(get_parser=no_parser, parse_fn=MagicMock())
        ctx = WriteHookContext(path="/test.bin", content=b"data", context=None)
        hook.on_post_write(ctx)

        # No thread should have been started
        with hook._lock:
            assert len([t for t in hook._threads if t.is_alive()]) == 0

    def test_starts_thread_for_parseable_file(self):
        parse_called = threading.Event()

        async def mock_parse(path: str, store_result: bool = False):
            parse_called.set()

        hook = AutoParseWriteHook(
            get_parser=lambda p: "parser-stub",
            parse_fn=mock_parse,
        )
        ctx = WriteHookContext(path="/test.py", content=b"print('hello')", context=None)
        hook.on_post_write(ctx)

        # Wait for thread to complete (or timeout)
        parse_called.wait(timeout=5.0)
        assert parse_called.is_set()

    def test_shutdown_no_threads(self):
        hook = AutoParseWriteHook(get_parser=MagicMock(), parse_fn=MagicMock())
        stats = hook.shutdown(timeout=1.0)
        assert stats["total_threads"] == 0

    def test_shutdown_waits_for_threads(self):
        done = threading.Event()

        async def slow_parse(path: str, store_result: bool = False):
            done.wait(timeout=5.0)

        hook = AutoParseWriteHook(
            get_parser=lambda p: "stub",
            parse_fn=slow_parse,
        )
        ctx = WriteHookContext(path="/test.py", content=b"code", context=None)
        hook.on_post_write(ctx)

        # Let thread start
        time.sleep(0.1)

        # Signal completion
        done.set()

        stats = hook.shutdown(timeout=5.0)
        assert stats["total_threads"] == 1
        assert stats["completed"] == 1
        assert stats["timed_out"] == 0

    def test_name(self):
        hook = AutoParseWriteHook(get_parser=MagicMock(), parse_fn=MagicMock())
        assert hook.name == "auto_parse"


# =========================================================================
# TigerCacheRenameHook
# =========================================================================


class TestTigerCacheRenameHook:
    def _make_tiger_cache(
        self,
        old_grants: list[dict] | None = None,
        new_grants: list[dict] | None = None,
    ) -> MagicMock:
        tc = MagicMock()
        tc.get_directory_grants_for_path = MagicMock(
            side_effect=lambda path, zone: old_grants or [] if "old" in path else new_grants or []
        )
        tc._resource_map = MagicMock()
        tc._resource_map.get_or_create_int_id = MagicMock(return_value=42)
        return tc

    def test_skips_if_no_tiger_cache(self):
        hook = TigerCacheRenameHook(tiger_cache=None)
        ctx = RenameHookContext(old_path="/old/file.txt", new_path="/new/file.txt", context=None)
        hook.on_post_rename(ctx)  # should not raise

    def test_skips_if_no_grant_changes(self):
        tc = self._make_tiger_cache(
            old_grants=[{"subject_type": "user", "subject_id": "alice", "permission": "read"}],
            new_grants=[{"subject_type": "user", "subject_id": "alice", "permission": "read"}],
        )
        hook = TigerCacheRenameHook(tiger_cache=tc)
        ctx = RenameHookContext(
            old_path="/old/file.txt", new_path="/new/file.txt", context=None, zone_id="z1"
        )
        hook.on_post_rename(ctx)

        # No bitmap operations since grants are the same
        tc.remove_from_bitmap.assert_not_called()
        tc.add_to_bitmap.assert_not_called()

    def test_removes_from_old_grants(self):
        tc = self._make_tiger_cache(
            old_grants=[{"subject_type": "user", "subject_id": "alice", "permission": "read"}],
            new_grants=[],
        )
        hook = TigerCacheRenameHook(tiger_cache=tc)
        ctx = RenameHookContext(
            old_path="/old/file.txt",
            new_path="/new/file.txt",
            context=None,
            zone_id="z1",
        )
        hook.on_post_rename(ctx)

        tc.remove_from_bitmap.assert_called_once()
        tc.add_to_bitmap.assert_not_called()

    def test_adds_to_new_grants(self):
        tc = self._make_tiger_cache(
            old_grants=[],
            new_grants=[
                {
                    "subject_type": "user",
                    "subject_id": "bob",
                    "permission": "write",
                    "include_future_files": True,
                }
            ],
        )
        hook = TigerCacheRenameHook(tiger_cache=tc)
        ctx = RenameHookContext(
            old_path="/old/file.txt",
            new_path="/new/file.txt",
            context=None,
            zone_id="z1",
        )
        hook.on_post_rename(ctx)

        tc.add_to_bitmap.assert_called_once()
        tc.persist_single_grant.assert_called_once()

    def test_directory_rename_lists_children(self):
        tc = self._make_tiger_cache(
            old_grants=[{"subject_type": "user", "subject_id": "alice", "permission": "read"}],
            new_grants=[],
        )

        # Mock metadata listing with 2 child files
        child1 = MagicMock()
        child1.path = "/new/dir/a.txt"
        child2 = MagicMock()
        child2.path = "/new/dir/b.txt"

        hook = TigerCacheRenameHook(
            tiger_cache=tc,
            metadata_list_iter=lambda **kwargs: [child1, child2],
        )
        ctx = RenameHookContext(
            old_path="/old/dir",
            new_path="/new/dir",
            context=None,
            zone_id="z1",
            is_directory=True,
        )
        hook.on_post_rename(ctx)

        # remove_from_bitmap called for each child file
        assert tc.remove_from_bitmap.call_count == 2

    def test_name(self):
        hook = TigerCacheRenameHook(tiger_cache=None)
        assert hook.name == "tiger_cache_rename"

    def test_no_resource_map_skips(self):
        tc = MagicMock()
        tc.get_directory_grants_for_path = MagicMock(
            side_effect=lambda path, zone: (
                [{"subject_type": "user", "subject_id": "x", "permission": "r"}]
                if "old" in path
                else []
            )
        )
        tc._resource_map = None  # no resource map

        hook = TigerCacheRenameHook(tiger_cache=tc)
        ctx = RenameHookContext(
            old_path="/old/f.txt", new_path="/new/f.txt", context=None, zone_id="z1"
        )
        hook.on_post_rename(ctx)  # should not raise
