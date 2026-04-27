"""Tests for ReadaheadManager baseline behaviour.

Mount-level `io_profile` was retired in R20.16.8 (Issue #1413 orphan).
These tests now cover only the baseline readahead defaults — the
profile-driven windowing variants were dropped with the knob.
"""

from unittest.mock import MagicMock

from nexus.fuse.ops._shared import read_range_from_backend
from nexus.fuse.readahead import ReadaheadConfig, ReadaheadManager, ReadSession


class TestReadSessionDefaults:
    """Test ReadSession window defaults and explicit overrides."""

    def test_custom_initial_window(self) -> None:
        session = ReadSession(
            path="/test",
            fh=1,
            readahead_window=256 * 1024,
            max_window=1 * 1024 * 1024,
        )
        assert session.readahead_window == 256 * 1024
        assert session.max_window == 1 * 1024 * 1024

    def test_default_window(self) -> None:
        session = ReadSession(path="/test", fh=1)
        # Default from module constants
        assert session.readahead_window == 512 * 1024
        assert session.max_window == 64 * 1024 * 1024


class TestReadaheadManagerOnOpen:
    """Test on_open() creates sessions with the manager's config."""

    def _make_manager(self, config: ReadaheadConfig | None = None) -> ReadaheadManager:
        if config is None:
            config = ReadaheadConfig(enabled=True, prefetch_on_open=True)
        read_func = MagicMock(return_value=b"data")
        return ReadaheadManager(config=config, read_func=read_func)

    def test_on_open_creates_session_with_manager_defaults(self) -> None:
        config = ReadaheadConfig(enabled=True, prefetch_on_open=True)
        manager = self._make_manager(config)
        manager.on_open(fh=3, path="/default", file_size=1000)

        session = manager._sessions.get(3)
        assert session is not None
        # Uses manager's default config
        assert session.max_window == manager._config.max_window


class TestReadRangeFromBackend:
    """Regression coverage for FUSE readahead range fetches."""

    def test_uses_nexus_read_range_without_full_file_read(self) -> None:
        ctx = MagicMock()
        ctx.context = object()
        ctx.nexus_fs.read_range.return_value = b"chunk"

        result = read_range_from_backend(ctx, "/large.bin", 8, 5)

        assert result == b"chunk"
        ctx.nexus_fs.read_range.assert_called_once_with(
            "/large.bin",
            8,
            13,
            context=ctx.context,
        )
        ctx.nexus_fs.sys_read.assert_not_called()
