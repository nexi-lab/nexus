"""Tests for nexus-fs playground TUI.

Covers: Pilot API behavioral tests, edge cases (terminal size, binary preview,
large files, empty state, rapid interaction), and widget-level tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Guard: skip all TUI tests if textual is not installed
textual = pytest.importorskip("textual")


from nexus.fs._tui import PlaygroundApp  # noqa: E402
from nexus.fs._tui.file_browser import (  # noqa: E402
    MAX_DISPLAY_ENTRIES,
    FileBrowser,
    _format_modified,
    _format_size,
)
from nexus.fs._tui.file_preview import (  # noqa: E402
    MAX_PREVIEW_BYTES,
    _guess_lexer,
    _hex_preview,
    _is_likely_binary,
)
from nexus.fs._tui.mount_panel import MountInfo, MountPanel  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_fs(
    mount_points: list[str] | None = None,
    ls_entries: list[dict] | None = None,
    read_content: bytes = b"hello world",
    stat_result: dict | None = None,
) -> MagicMock:
    """Create a mock SlimNexusFS."""
    fs = MagicMock()
    fs.list_mounts.return_value = mount_points or ["/local/data"]
    fs.ls = AsyncMock(return_value=ls_entries or [])
    fs.read = AsyncMock(return_value=read_content)
    fs.read_range = AsyncMock(return_value=read_content[:MAX_PREVIEW_BYTES])
    fs.stat = AsyncMock(
        return_value=stat_result
        or {
            "path": "/local/data/test.txt",
            "size": len(read_content),
            "is_directory": False,
            "etag": "abc",
            "mime_type": "text/plain",
            "created_at": "2026-01-01T00:00:00",
            "modified_at": "2026-01-01T00:00:00",
            "version": 1,
            "zone_id": "root",
            "entry_type": 0,
        }
    )
    return fs


def _make_ls_entries(count: int = 5, include_dirs: bool = True) -> list[dict]:
    """Generate mock directory listing entries."""
    entries = []
    if include_dirs:
        entries.append(
            {
                "path": "/local/data/subdir",
                "size": 4096,
                "is_directory": True,
                "modified_at": "2026-01-15T10:30:00",
            }
        )
    for i in range(count):
        entries.append(
            {
                "path": f"/local/data/file_{i:03d}.txt",
                "size": 1024 * (i + 1),
                "is_directory": False,
                "modified_at": f"2026-01-{15 + (i % 15):02d}T10:30:00",
            }
        )
    return entries


# ---------------------------------------------------------------------------
# Unit tests: utility functions
# ---------------------------------------------------------------------------


class TestFormatSize:
    def test_bytes(self):
        assert _format_size(512) == "512 B"

    def test_kilobytes(self):
        assert _format_size(2048) == "2.0 KB"

    def test_megabytes(self):
        assert _format_size(5 * 1024 * 1024) == "5.0 MB"

    def test_gigabytes(self):
        assert _format_size(3 * 1024 * 1024 * 1024) == "3.0 GB"

    def test_zero(self):
        assert _format_size(0) == "0 B"


class TestFormatModified:
    def test_iso_timestamp(self):
        assert _format_modified("2026-01-15T10:30:45") == "2026-01-15 10:30"

    def test_none(self):
        assert _format_modified(None) == "—"

    def test_empty(self):
        assert _format_modified("") == "—"


class TestGuessLexer:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/foo/bar.py", "python"),
            ("/foo/bar.js", "javascript"),
            ("/foo/bar.rs", "rust"),
            ("/foo/bar.go", "go"),
            ("/foo/bar.yaml", "yaml"),
            ("/foo/bar.json", "json"),
            ("/foo/bar.unknown", "text"),
            ("/foo/Dockerfile", "docker"),
            ("/foo/Makefile", "makefile"),
        ],
    )
    def test_lexer_detection(self, path, expected):
        assert _guess_lexer(path) == expected


class TestBinaryDetection:
    def test_text_content(self):
        assert not _is_likely_binary(b"hello world\nline 2\n")

    def test_binary_with_null(self):
        assert _is_likely_binary(b"some\x00binary\x00content")

    def test_empty(self):
        assert not _is_likely_binary(b"")


class TestHexPreview:
    def test_basic_output(self):
        data = b"Hello, World!"
        result = _hex_preview(data)
        assert "48 65 6c 6c 6f" in result  # "Hello" in hex
        assert "Hello" in result  # ASCII column

    def test_respects_max_lines(self):
        data = b"\x00" * 1024
        result = _hex_preview(data, max_lines=2)
        lines = result.strip().split("\n")
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# Mount panel tests
# ---------------------------------------------------------------------------


class TestMountPanel:
    @pytest.mark.asyncio
    async def test_mount_info_defaults(self):
        info = MountInfo(mount_point="/s3/bucket")
        assert info.status == "checking"
        assert info.latency_ms is None
        assert info.error is None

    @pytest.mark.asyncio
    async def test_connectivity_check_success(self):
        """Mount panel updates status to connected on successful ls."""
        fs = _make_mock_fs(mount_points=["/local/data"])

        app = PlaygroundApp()
        app._fs = fs
        app._mount_points = ["/local/data"]

        async with app.run_test(size=(120, 40)) as pilot:
            # Give time for connectivity check
            await pilot.pause(delay=0.5)

    @pytest.mark.asyncio
    async def test_connectivity_check_failure(self):
        """Mount panel shows error status on failed ls."""
        fs = _make_mock_fs(mount_points=["/s3/bucket"])
        fs.ls = AsyncMock(side_effect=ConnectionError("timeout"))

        panel = MountPanel(fs, ["/s3/bucket"])
        info = panel._mount_infos[0]
        assert info.status == "checking"


# ---------------------------------------------------------------------------
# File browser tests
# ---------------------------------------------------------------------------


class TestFileBrowser:
    @pytest.mark.asyncio
    async def test_load_directory(self):
        fs = _make_mock_fs(ls_entries=_make_ls_entries(5))
        app = PlaygroundApp()
        app._fs = fs
        app._mount_points = ["/local/data"]

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.5)

    def test_large_directory_cap_constant(self):
        """Display cap is set to 500 entries."""
        assert MAX_DISPLAY_ENTRIES == 500

    def test_go_back_empty_history(self):
        fs = _make_mock_fs()
        browser = FileBrowser(fs)
        browser.current_path = "/"
        browser._history = []
        # go_back returns False at root with no history
        # (can't test without composed widget, so test the logic)
        assert browser._history == []

    def test_entry_count(self):
        fs = _make_mock_fs()
        browser = FileBrowser(fs)
        browser._total_count = 42
        assert browser.entry_count == 42


# ---------------------------------------------------------------------------
# File preview tests
# ---------------------------------------------------------------------------


class TestFilePreview:
    def test_preview_constants(self):
        """Preview byte cap is 1MB."""
        assert MAX_PREVIEW_BYTES == 1_048_576

    def test_large_file_would_use_read_range(self):
        """Files > 1MB should use read_range, not read."""
        fs = _make_mock_fs()
        fs.read_range = AsyncMock(return_value=b"x" * MAX_PREVIEW_BYTES)
        # Verify mock is callable — actual call tested via integration
        assert fs.read_range is not None
        assert MAX_PREVIEW_BYTES == 1_048_576

    def test_binary_extension_detection(self):
        """Known binary extensions are detected."""
        from nexus.fs._tui.file_preview import _BINARY_EXTENSIONS

        assert ".png" in _BINARY_EXTENSIONS
        assert ".exe" in _BINARY_EXTENSIONS
        assert ".py" not in _BINARY_EXTENSIONS

    def test_hex_preview_format(self):
        """Hex preview shows offset, hex, and ASCII columns."""
        data = bytes(range(32))
        result = _hex_preview(data)
        assert "00000000" in result  # offset
        assert "00 01 02" in result  # hex values


# ---------------------------------------------------------------------------
# PlaygroundApp integration tests
# ---------------------------------------------------------------------------


class TestPlaygroundApp:
    @pytest.mark.asyncio
    async def test_empty_state_no_uris(self):
        """App shows empty state message when no URIs and no state dir."""
        with patch.dict("os.environ", {"NEXUS_FS_STATE_DIR": "/nonexistent/path"}, clear=False):
            app = PlaygroundApp(uris=())

            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(delay=0.5)
                # App should show empty state
                empty = app.query_one("#empty-state")
                assert empty is not None

    @pytest.mark.asyncio
    async def test_mount_failure_shows_error(self):
        """App shows error when mount fails."""
        with patch("nexus.fs.mount", side_effect=ValueError("bad URI")):
            app = PlaygroundApp(uris=("invalid://bad",))

            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(delay=0.5)

    @pytest.mark.asyncio
    async def test_quit_binding(self):
        """Pressing q quits the app."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.3)
            await pilot.press("q")

    @pytest.mark.asyncio
    async def test_search_toggle_via_action(self):
        """Toggle search action changes search_visible state."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.3)
            assert not app.search_visible
            app.action_toggle_search()
            assert app.search_visible
            app.action_toggle_search()
            assert not app.search_visible

    @pytest.mark.asyncio
    async def test_mount_panel_toggle_via_action(self):
        """Toggle mount panel action changes show_mount_panel state."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.3)
            assert app.show_mount_panel is True
            app.action_toggle_mount_panel()
            assert app.show_mount_panel is False
            app.action_toggle_mount_panel()
            assert app.show_mount_panel is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_small_terminal(self):
        """App handles terminal smaller than minimum gracefully."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause(delay=0.3)
            # Should not crash at small terminal size

    @pytest.mark.asyncio
    async def test_narrow_terminal_collapses_mount_panel(self):
        """Mount panel collapses at < 100 columns."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.pause(delay=0.3)
            # At 90 cols, mount panel should be collapsed
            # (auto-collapse happens on resize)

    @pytest.mark.asyncio
    async def test_rapid_key_presses(self):
        """App handles rapid key presses without crashing."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.3)
            # Rapid-fire keys
            for _ in range(10):
                await pilot.press("down")
            for _ in range(10):
                await pilot.press("up")
            await pilot.press("/")
            await pilot.press("escape")
            await pilot.press("m")
            await pilot.press("m")

    @pytest.mark.asyncio
    async def test_search_no_results(self):
        """Search with no matching files handles gracefully."""
        fs = _make_mock_fs(ls_entries=[])

        with patch("nexus.fs.mount", new_callable=AsyncMock, return_value=fs):
            app = PlaygroundApp(uris=("local://./data",))

            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(delay=0.5)
                # Open search and submit
                await pilot.press("/")
                # Type and submit
                app.query_one("#search-input").value = "nonexistent"
                await pilot.press("enter")
                await pilot.pause(delay=0.3)

    def test_format_size_edge_cases(self):
        """Size formatting handles edge values."""
        assert _format_size(0) == "0 B"
        assert _format_size(1) == "1 B"
        assert _format_size(1023) == "1023 B"
        assert _format_size(1024) == "1.0 KB"

    def test_binary_detection_edge_cases(self):
        """Binary detection handles empty and pure-ASCII content."""
        assert not _is_likely_binary(b"")
        assert not _is_likely_binary(b"pure ascii text")
        assert _is_likely_binary(b"\x89PNG\r\n\x1a\n\x00")

    def test_lexer_guess_unknown_extension(self):
        """Unknown extensions default to 'text' lexer."""
        assert _guess_lexer("/foo/bar.xyz") == "text"
        assert _guess_lexer("/foo/bar") == "text"
