"""Unit tests for DynamicViewerReadHook."""

from __future__ import annotations

from nexus.contracts.vfs_hooks import ReadHookContext
from nexus.services.rebac.dynamic_viewer_hook import DynamicViewerReadHook


class TestDynamicViewerReadHook:
    def _make_hook(
        self,
        subject: tuple[str, str] | None = ("user", "alice"),
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
            get_subject=lambda ctx: ("user", "alice"),
            get_viewer_config=lambda s, p: {"columns": ["col1"]},
            apply_filter=lambda data, cfg, fmt: {"filtered_data": b"raw-bytes"},
        )
        ctx = ReadHookContext(path="/data.CSV", context=None, content=b"original")
        hook.on_post_read(ctx)
        assert ctx.content == b"raw-bytes"

    def test_name(self):
        hook = self._make_hook()
        assert hook.name == "dynamic_viewer"
