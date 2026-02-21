"""Unit tests for AutoParseWriteHook."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from nexus.contracts.vfs_hooks import WriteHookContext
from nexus.parsers.auto_parse_hook import AutoParseWriteHook


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
