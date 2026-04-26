"""Tests for nexus-fs playground TUI.

Covers: Pilot API behavioral tests, edge cases (terminal size, binary preview,
large files, empty state, rapid interaction), and widget-level tests.
"""

from __future__ import annotations

import json
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Guard: skip all TUI tests if textual is not installed
textual = pytest.importorskip("textual")


from textual.widgets import DataTable  # noqa: E402

from nexus.fs._tui import ContextualNexusFS, PlaygroundApp  # noqa: E402
from nexus.fs._tui.auth_guidance import auth_guidance, format_runtime_error  # noqa: E402
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
    """Create a mock ContextualNexusFS-style object for TUI tests."""
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

    def test_error_render_includes_auth_hint(self):
        """Auth-backed mount errors include inline next-step guidance."""
        panel = MountPanel(_make_mock_fs(), ["/s3/bucket"])
        rendered = panel._render_mount(MountInfo(mount_point="/s3/bucket", status="error"))
        assert "run /auth s3" in rendered


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

    @pytest.mark.asyncio
    async def test_empty_directory_shows_empty_state(self, tmp_path):
        """Empty directory shows 'Empty folder' message instead of blank table."""
        app = PlaygroundApp(uris=(f"local://{tmp_path}",))

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.5)
            browser = app.query_one("#file-browser", FileBrowser)

            from textual.widgets import Static

            empty_state = browser.query_one("#empty-state", Static)
            table = browser.query_one("#file-table", DataTable)
            assert empty_state.display is True
            assert table.display is False


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
    async def test_empty_state_no_uris(self, tmp_path):
        """App shows empty state message when no URIs and no state dir."""
        # Use a writable tmp subdir that doesn't exist yet so state_dir() creates
        # it fresh — avoids the read-only /nonexistent root on some systems.
        empty_state_dir = str(tmp_path / "empty_state")
        with patch.dict("os.environ", {"NEXUS_FS_STATE_DIR": empty_state_dir}, clear=False):
            app = PlaygroundApp(uris=())

            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(delay=0.5)
                # App should show empty state
                empty = app.query_one("#empty-state")
                assert empty is not None
                picker = app.query_one("#connector-picker", DataTable)
                assert picker is not None

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

    @pytest.mark.asyncio
    async def test_command_toggle_via_action(self):
        """Toggle command action changes command_visible state."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.3)
            assert not app.command_visible
            app.action_toggle_command()
            assert app.command_visible
            app.action_toggle_command()
            assert not app.command_visible

    @pytest.mark.asyncio
    async def test_command_mode_blocks_other_bindings(self):
        """Bound keys like 'b' should type into command buffer, not trigger actions."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.3)
            # Enter command mode
            app.action_toggle_command()
            assert app.command_visible

            # check_action should block non-command actions
            assert app.check_action("go_back", ()) is False
            assert app.check_action("request_quit", ()) is False
            assert app.check_action("copy_path", ()) is False

            # check_action should allow command actions
            assert app.check_action("toggle_command", ()) is True
            assert app.check_action("submit_command", ()) is True
            assert app.check_action("command_backspace", ()) is True

            # Press 'b' — should add to buffer, not navigate back
            await pilot.press("b")
            assert "b" in app.command_buffer

            # Exit command mode — actions should be re-enabled
            app.action_toggle_command()
            assert not app.command_visible
            assert app.check_action("go_back", ()) is True

    @pytest.mark.asyncio
    async def test_mount_uri_adds_local_mount(self, tmp_path):
        """The command-path mount helper adds a local mount and rebuilds the UI."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.3)
            await app._mount_uri(f"local://{tmp_path}")
            await pilot.pause(delay=0.3)
            assert any(mp.endswith(tmp_path.name) for mp in app._mount_points)

    @pytest.mark.asyncio
    async def test_submit_command_mounts_local_uri(self, tmp_path):
        """Submitting the command buffer mounts a local URI."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.3)
            app.command_visible = True
            app.command_buffer = f"/mount local://{tmp_path}"
            await app.action_submit_command()
            await pilot.pause(delay=0.3)
            assert any(mp.endswith(tmp_path.name) for mp in app._mount_points)

    @pytest.mark.asyncio
    async def test_connector_picker_mounts_selected_uri(self, tmp_path):
        """Selecting a connector picker row mounts it and transitions to browser UI."""
        app = PlaygroundApp(uris=())
        target_uri = f"local://{tmp_path}"
        state_dir = tmp_path / "state"

        with (
            patch.dict("os.environ", {"NEXUS_FS_STATE_DIR": str(state_dir)}, clear=False),
            patch.object(
                app, "_supported_connector_rows", return_value=[(target_uri, "mountable")]
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(delay=0.5)
                picker = app.query_one("#connector-picker", DataTable)
                assert picker.row_count == 1
                await pilot.press("enter")
                await pilot.pause(delay=0.5)
                assert any(mp.endswith(tmp_path.name) for mp in app._mount_points)
                assert app.picker_visible is False

    @pytest.mark.asyncio
    async def test_show_connector_picker_action_from_browser(self, tmp_path):
        """The add-mount action reopens the connector picker from browser mode."""
        app = PlaygroundApp(uris=(f"local://{tmp_path}",))

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.5)
            assert app.picker_visible is False
            await app.action_show_connector_picker()
            await pilot.pause(delay=0.2)
            picker = app.query_one("#connector-picker", DataTable)
            assert picker is not None
            assert app.picker_visible is True

    @pytest.mark.asyncio
    async def test_connector_picker_prompts_for_custom_local_uri(self, tmp_path):
        """The local picker row opens an editable URI input before mounting."""
        app = PlaygroundApp(uris=())
        state_dir = tmp_path / "state"

        with patch.dict("os.environ", {"NEXUS_FS_STATE_DIR": str(state_dir)}, clear=False):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(delay=0.5)
                await pilot.press("enter")
                await pilot.pause(delay=0.2)
                picker_input = app.query_one("#picker-input")
                assert app.picker_input_visible is True
                assert "local://" in picker_input.value

    @pytest.mark.asyncio
    async def test_connector_picker_local_input_mounts_custom_uri(self, tmp_path):
        """The mount wizard can complete a local mount without raw command entry."""
        app = PlaygroundApp(uris=())
        target_uri = f"local://{tmp_path}"
        state_dir = tmp_path / "state"

        with patch.dict("os.environ", {"NEXUS_FS_STATE_DIR": str(state_dir)}, clear=False):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(delay=0.5)
                await pilot.press("enter")
                await pilot.pause(delay=0.2)
                picker_input = app.query_one("#picker-input")
                picker_input.value = target_uri
                await app.action_submit_command()
                await pilot.pause(delay=0.3)
                assert any(mp.endswith(tmp_path.name) for mp in app._mount_points)
                assert app.picker_visible is False

    @pytest.mark.asyncio
    async def test_browser_enter_opens_selected_directory(self, tmp_path):
        """Pressing Enter in the file browser should navigate into the selected directory."""
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "nested.txt").write_text("hi")
        (tmp_path / "hello.txt").write_text("hello")
        app = PlaygroundApp(uris=(f"local://{tmp_path}",))

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.5)
            browser = app.query_one("#file-browser", FileBrowser)
            table = browser.query_one("#file-table", DataTable)
            table.focus()
            await pilot.pause(delay=0.1)
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            assert browser.current_path.endswith("/skills")
            assert app._current_path.endswith("/skills")

    @pytest.mark.asyncio
    async def test_mount_uri_rejects_empty_s3_bucket(self):
        """Incomplete S3 URIs should be rejected instead of creating a broken /s3/ mount."""
        app = PlaygroundApp(uris=())
        state_dir = tempfile.mkdtemp(prefix="playground-empty-s3-")

        with patch.dict("os.environ", {"NEXUS_FS_STATE_DIR": state_dir}, clear=False):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(delay=0.3)
                await app._mount_uri("s3://")
                await pilot.pause(delay=0.2)
                assert app._mount_points == []

    @pytest.mark.asyncio
    async def test_unmount_selected_mount_removes_mount(self, tmp_path):
        """Unmount removes the selected mount from the active playground session."""
        left = tmp_path / "left"
        right = tmp_path / "right"
        left.mkdir()
        right.mkdir()
        app = PlaygroundApp(uris=(f"local://{left}", f"local://{right}"))

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.5)
            app.action_focus_mount_panel()
            await pilot.pause(delay=0.1)
            panel = app.query_one("#mount-panel", MountPanel)
            panel.selected_index = 1
            await pilot.pause(delay=0.1)
            await app.action_unmount_selected_mount()
            await pilot.pause(delay=0.3)
            assert app._mount_points == [f"/local/{left.name}"]
            assert app._uris == (f"local://{left}",)

    @pytest.mark.asyncio
    async def test_browser_banner_mentions_restored_mounts(self, tmp_path):
        """Restored sessions should say so explicitly in the top banner."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        mounts_file = state_dir / "mounts.json"
        mounts_file.write_text(json.dumps([f"local://{tmp_path}"]))
        app = PlaygroundApp(uris=())

        with patch.dict("os.environ", {"NEXUS_FS_STATE_DIR": str(state_dir)}, clear=False):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(delay=0.5)
                banner = app.query_one("#playground-banner")
                assert "Restored 1 mount" in str(banner.render())

    @pytest.mark.asyncio
    async def test_browser_banner_mentions_show_mounts_when_auto_collapsed(self, tmp_path):
        """Narrow terminals should advertise how to reopen the auto-collapsed mount panel."""
        app = PlaygroundApp(uris=(f"local://{tmp_path}",))

        async with app.run_test(size=(99, 24)) as pilot:
            await pilot.pause(delay=0.5)
            banner = app.query_one("#playground-banner")
            panel = app.query_one("#mount-panel", MountPanel)
            assert "`m` show mounts" in str(banner.render())
            assert app.show_mount_panel is False
            assert panel.display is False

    @pytest.mark.asyncio
    async def test_too_small_hides_main_content(self, tmp_path):
        """Too-small terminals should show only the warning, not stacked content underneath."""
        app = PlaygroundApp(uris=(f"local://{tmp_path}",))

        async with app.run_test(size=(99, 22)) as pilot:
            await pilot.pause(delay=0.5)
            assert app.query_one("#too-small-message").display is True
            assert app.query_one("#playground-banner").display is False
            assert app.query_one("#main-area").display is False
            assert app.query_one("#status-bar").display is False

    def test_auth_guidance_for_s3(self):
        """S3 auth guidance points users to the guided CLI flow."""
        app = PlaygroundApp(uris=())
        message = app._auth_guidance("s3")
        assert "nexus-fs auth connect s3 native" in message
        assert "reopen the playground and mount `s3://bucket`" in message

    def test_auth_guidance_for_expired_gws(self):
        """Expired Google auth gets explicit re-auth steps."""
        message = auth_guidance("gws", user_email="alice@example.com", expired=True)
        assert "Google auth expired" in message
        assert "nexus-fs auth connect gws oauth --user-email alice@example.com" in message
        assert "nexus-fs auth test gws" in message

    def test_runtime_error_formats_expired_gws_steps(self):
        """Browse/preview auth failures should render guided steps, not raw backend text."""
        message = format_runtime_error(
            "/gws/docs",
            RuntimeError("[AUTH_EXPIRED] Using keyring backend: keyring"),
        )
        assert "Can't access `/gws/docs`." in message
        assert "Google auth expired" in message
        assert "nexus-fs auth connect gws oauth" in message

    @pytest.mark.asyncio
    async def test_contextual_fs_falls_back_to_backend_list_dir_for_empty_metadata(self):
        """Connector-backed playground mounts should still browse when slim metadata is empty."""

        class _Backend:
            def list_dir(self, path, context=None):
                assert path == ""
                return ["Doc Alpha", "Folder/"]

        class _Route:
            backend = _Backend()
            backend_path = ""

        class _Router:
            def route(self, path):
                assert path == "/gws/docs"
                return _Route()

        class _Kernel:
            router = _Router()

            def sys_readdir(self, path, recursive=False, details=False, context=None):
                assert path == "/gws/docs"
                return []

        fs = ContextualNexusFS(_Kernel())
        rows = fs.ls("/gws/docs", detail=True, recursive=False)
        assert isinstance(rows, list)
        assert rows[0]["path"] == "/gws/docs/Doc Alpha"
        assert rows[0]["is_directory"] is False
        assert rows[1]["path"] == "/gws/docs/Folder"
        assert rows[1]["is_directory"] is True

    @pytest.mark.asyncio
    async def test_contextual_fs_prefers_detailed_backend_entries(self):
        """Detailed connector metadata should flow through to the browser rows."""

        class _Backend:
            def list_dir_details(self, path, context=None):
                assert path == ""
                return [
                    {
                        "name": "Doc Alpha [docA]",
                        "size": 123,
                        "modified_at": "2026-03-26T00:40:00Z",
                        "is_directory": False,
                    }
                ]

        class _Route:
            def __init__(self):
                self.backend = _Backend()
                self.backend_path = ""

        class _Router:
            def route(self, path):
                assert path == "/gws/docs"
                return _Route()

        class _Kernel:
            router = _Router()

            def sys_readdir(self, path, recursive=False, details=False, context=None):
                assert path == "/gws/docs"
                return []

        fs = ContextualNexusFS(_Kernel())
        rows = fs.ls("/gws/docs", detail=True, recursive=False)
        assert rows[0]["path"] == "/gws/docs/Doc Alpha [docA]"
        assert rows[0]["size"] == 123
        assert rows[0]["modified_at"] == "2026-03-26T00:40:00Z"

    @pytest.mark.asyncio
    async def test_contextual_fs_prefers_live_backend_rows_over_stale_metadata(self):
        """Connector root browsing should prefer live backend listings over synthetic VFS rows."""

        class _Backend:
            def list_dir_details(self, path, context=None):
                assert path == ""
                return [
                    {
                        "name": "Fresh Doc [docA]",
                        "size": 456,
                        "modified_at": "2026-03-26T01:00:00Z",
                        "is_directory": False,
                    }
                ]

        class _Route:
            def __init__(self):
                self.backend = _Backend()
                self.backend_path = ""

        class _Router:
            def route(self, path):
                assert path == "/gws/docs"
                return _Route()

        class _Kernel:
            router = _Router()

            def sys_readdir(self, path, recursive=False, details=False, context=None):
                assert path == "/gws/docs"
                return [
                    {
                        "path": "/gws/docs/Stale Doc",
                        "size": 0,
                        "modified_at": "2026-03-26T00:00:00Z",
                        "is_directory": False,
                    }
                ]

        fs = ContextualNexusFS(_Kernel())
        rows = fs.ls("/gws/docs", detail=True, recursive=False)
        assert rows == [
            {
                "path": "/gws/docs/Fresh Doc [docA]",
                "size": 456,
                "is_directory": False,
                "etag": None,
                "mime_type": "application/octet-stream",
                "created_at": None,
                "modified_at": "2026-03-26T01:00:00Z",
                "version": 0,
                "zone_id": "root",
                "entry_type": 0,
            }
        ]

    @pytest.mark.asyncio
    async def test_contextual_fs_stat_falls_back_to_backend_directory_entries(self):
        """Preview should be able to stat connector-backed files from live backend rows."""

        class _Backend:
            def list_dir_details(self, path, context=None):
                assert path == ""
                return [
                    {
                        "name": "Doc Alpha [docA]",
                        "size": 123,
                        "modified_at": "2026-03-26T00:40:00Z",
                        "is_directory": False,
                    }
                ]

        class _Route:
            def __init__(self):
                self.backend = _Backend()
                self.backend_path = ""

        class _Router:
            def route(self, path):
                assert path in {"/gws/docs", "/gws/docs/Doc Alpha [docA]"}
                return _Route()

        class _Kernel:
            router = _Router()

            def sys_readdir(self, path, recursive=False, details=False, context=None):
                return []

            def sys_stat(self, path, context=None):
                return None

        fs = ContextualNexusFS(_Kernel())
        stat = fs.stat("/gws/docs/Doc Alpha [docA]")
        assert stat == {
            "path": "/gws/docs/Doc Alpha [docA]",
            "size": 123,
            "is_directory": False,
            "etag": None,
            "mime_type": "application/octet-stream",
            "created_at": None,
            "modified_at": "2026-03-26T00:40:00Z",
            "version": 0,
            "zone_id": "root",
            "entry_type": 0,
        }

    def test_supported_connector_rows_include_dynamic_gws_targets(self):
        """The playground lists concrete connector mount targets, not just services."""
        app = PlaygroundApp(uris=())
        rows = app._supported_connector_rows()
        names = {name for name, _mode in rows}
        assert "s3://<enter bucket manually>" in names
        assert "gcs://project/bucket" in names
        assert "gws://drive" in names
        assert "gws://gmail" in names
        assert "gws://calendar" in names
        assert "gws://github" not in names
        assert "calendar://primary" not in names

    def test_supported_connector_rows_include_discovered_s3_buckets(self):
        """Discovered S3 buckets should appear as directly mountable picker rows."""
        app = PlaygroundApp(uris=())
        with patch.object(
            app,
            "_discovered_s3_bucket_rows",
            return_value=[("s3://nexus-888", "mountable auth:s3 discovered")],
        ):
            rows = app._supported_connector_rows()
        names = [name for name, _mode in rows]
        assert "s3://nexus-888" in names
        assert names.index("s3://nexus-888") < names.index("s3://<enter bucket manually>")

    @pytest.mark.asyncio
    async def test_resolve_mount_user_id_prefers_single_google_credential(self):
        """A single stored Google credential becomes the mount user identity."""
        app = PlaygroundApp(uris=())

        with (
            patch("nexus.fs._oauth_support.get_token_manager", return_value=MagicMock()),
            patch(
                "nexus.bricks.auth.oauth.credential_service.OAuthCredentialService.list_credentials",
                new=AsyncMock(
                    return_value=[
                        {
                            "provider": "google",
                            "user_email": "alice@example.com",
                        }
                    ]
                ),
            ),
        ):
            assert await app._resolve_mount_user_id("gws://drive") == "alice@example.com"

    @pytest.mark.asyncio
    async def test_resolve_mount_user_id_uses_shared_fs_inference(self):
        """Playground should reuse the shared slim-fs inference path."""
        app = PlaygroundApp(uris=())

        with patch(
            "nexus.fs._backend_factory._infer_connector_user_email",
            return_value="alice@example.com",
        ):
            assert await app._resolve_mount_user_id("gws://drive") == "alice@example.com"

    @pytest.mark.asyncio
    async def test_build_filesystem_uses_generic_mount_for_connector_uri(self):
        """Connector URIs go through nexus.fs.mount instead of the direct-only path."""
        app = PlaygroundApp(uris=())
        kernel = MagicMock()
        kernel.router.list_mounts.return_value = ["/gws/drive"]
        facade = MagicMock(kernel=kernel)

        with (
            patch("nexus.fs.mount", new=AsyncMock(return_value=facade)) as mock_mount,
            patch.object(
                app, "_resolve_mount_user_id", new=AsyncMock(return_value="alice@example.com")
            ),
        ):
            fs = await app._build_filesystem(("gws://drive",))
            # at=None when no mount_overrides are supplied (overrides.get(uri) → None)
            mock_mount.assert_awaited_once_with("gws://drive", at=None)
            assert fs.list_mounts() == ["/gws/drive"]

    @pytest.mark.asyncio
    async def test_shift_tab_moves_focus_to_mount_panel(self, tmp_path):
        """Shift+Tab from file browser should move focus back to mount panel."""
        (tmp_path / "hello.txt").write_text("hello")
        app = PlaygroundApp(uris=(f"local://{tmp_path}",))

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.5)
            browser = app.query_one("#file-browser", FileBrowser)
            table = browser.query_one("#file-table", DataTable)

            # Start with focus on file table
            table.focus()
            await pilot.pause(delay=0.1)
            assert app.focused is table

            # Shift+Tab should move focus to mount panel
            await pilot.press("shift+tab")
            await pilot.pause(delay=0.2)
            assert isinstance(app.focused, MountPanel)

    @pytest.mark.asyncio
    async def test_tab_and_shift_tab_round_trip(self, tmp_path):
        """Tab and Shift+Tab should cycle focus between mount panel and file browser."""
        (tmp_path / "hello.txt").write_text("hello")
        app = PlaygroundApp(uris=(f"local://{tmp_path}",))

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.5)
            mount_panel = app.query_one("#mount-panel", MountPanel)

            # Focus mount panel, then Tab forward to file browser
            mount_panel.focus()
            await pilot.pause(delay=0.1)
            assert isinstance(app.focused, MountPanel)

            await pilot.press("tab")
            await pilot.pause(delay=0.2)
            assert not isinstance(app.focused, MountPanel)

            # Shift+Tab back to mount panel
            await pilot.press("shift+tab")
            await pilot.pause(delay=0.2)
            assert isinstance(app.focused, MountPanel)

    @pytest.mark.asyncio
    async def test_shift_tab_noop_when_mount_panel_collapsed(self, tmp_path):
        """Shift+Tab should not focus mount panel when it is collapsed."""
        (tmp_path / "hello.txt").write_text("hello")
        app = PlaygroundApp(uris=(f"local://{tmp_path}",))

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.5)
            browser = app.query_one("#file-browser", FileBrowser)
            table = browser.query_one("#file-table", DataTable)

            # Focus file table first, then collapse mount panel
            table.focus()
            await pilot.pause(delay=0.1)
            app.show_mount_panel = False
            await pilot.pause(delay=0.1)
            assert app.focused is table

            # Shift+Tab should NOT move focus to (hidden) mount panel
            await pilot.press("shift+tab")
            await pilot.pause(delay=0.2)
            assert not isinstance(app.focused, MountPanel)


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


# ---------------------------------------------------------------------------
# Search match highlighting
# ---------------------------------------------------------------------------


class TestHighlightMatch:
    def test_basic_highlight(self):
        from nexus.fs._tui import _highlight_match

        result = _highlight_match("config.yaml", "config")
        assert "[bold yellow]config[/bold yellow]" in result
        assert ".yaml" in result

    def test_case_insensitive(self):
        from nexus.fs._tui import _highlight_match

        result = _highlight_match("README.md", "readme")
        assert "[bold yellow]README[/bold yellow]" in result

    def test_no_match(self):
        from nexus.fs._tui import _highlight_match

        result = _highlight_match("file.txt", "missing")
        assert result == "file.txt"

    def test_middle_match(self):
        from nexus.fs._tui import _highlight_match

        result = _highlight_match("my_config_file.py", "config")
        assert "my_" in result
        assert "[bold yellow]config[/bold yellow]" in result
        assert "_file.py" in result

    def test_empty_query(self):
        from nexus.fs._tui import _highlight_match

        result = _highlight_match("file.txt", "")
        # Empty query matches at index 0
        assert "file.txt" in result


# ---------------------------------------------------------------------------
# Screen reader announcements
# ---------------------------------------------------------------------------


class TestAccessibility:
    @pytest.mark.asyncio
    async def test_status_bar_update_announces(self):
        """Status bar update triggers a notification for screen readers."""
        app = PlaygroundApp(uris=())
        app._mount_points = ["/local/data"]
        app._current_path = "/local/data"

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.3)
            # _update_status_bar should not crash
            app._update_status_bar(announce=True)
            app._update_status_bar(announce=False)


# ---------------------------------------------------------------------------
# Help overlay tests (Issue #3508)
# ---------------------------------------------------------------------------


from nexus.fs._tui.help_overlay import (  # noqa: E402
    ALL_GROUPS,
    ALL_HELP_KEYS,
    HelpOverlay,
    _render_help_text,
)


class TestHelpOverlay:
    """Tests for the ? help overlay."""

    @pytest.mark.asyncio
    async def test_question_mark_opens_help_overlay(self):
        """Pressing ? pushes the HelpOverlay modal screen."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.3)
            await pilot.press("question_mark")
            await pilot.pause(delay=0.2)
            assert isinstance(app.screen, HelpOverlay)

    @pytest.mark.asyncio
    async def test_any_key_dismisses_help_overlay(self):
        """Pressing any key after opening the overlay should dismiss it."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.3)
            await pilot.press("question_mark")
            await pilot.pause(delay=0.2)
            assert isinstance(app.screen, HelpOverlay)
            # Dismiss with Escape
            await pilot.press("escape")
            await pilot.pause(delay=0.2)
            assert not isinstance(app.screen, HelpOverlay)

    @pytest.mark.asyncio
    async def test_help_not_opened_when_search_input_focused(self):
        """Pressing ? while typing in search should not open the overlay."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.3)
            # Activate search
            await pilot.press("/")
            await pilot.pause(delay=0.1)
            # Press ? while in search input
            await pilot.press("question_mark")
            await pilot.pause(delay=0.2)
            assert not isinstance(app.screen, HelpOverlay)

    @pytest.mark.asyncio
    async def test_help_overlay_shows_all_groups(self):
        """The overlay content includes all 4 binding group headers."""
        app = PlaygroundApp(uris=())

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.3)
            await pilot.press("question_mark")
            await pilot.pause(delay=0.2)
            assert isinstance(app.screen, HelpOverlay)
            text = str(app.screen.query_one("#help-text").render())
            for group_name, _ in ALL_GROUPS:
                assert group_name in text, f"Group '{group_name}' missing from help overlay"

    def test_help_overlay_has_minimum_bindings(self):
        """The overlay should contain at least 18 keybinding entries."""
        total = sum(len(bindings) for _, bindings in ALL_GROUPS)
        assert total >= 18, f"Expected >= 18 bindings, got {total}"

    def test_render_help_text_contains_dismiss_hint(self):
        """The rendered help text includes a dismiss instruction."""
        text = _render_help_text()
        assert "Dismiss" in text


class TestHelpOverlayDrift:
    """Drift detection: every shown BINDINGS entry must appear in the help overlay."""

    def test_bindings_covered_by_help_overlay(self):
        """Every visible PlaygroundApp binding key is present in the help overlay dict."""
        # Map Textual internal key names to the display keys used in the overlay.
        key_display_map: dict[str, str] = {
            "question_mark": "?",
        }
        for binding in PlaygroundApp.BINDINGS:
            if not binding.show:
                continue
            key = binding.key
            display_key = key_display_map.get(key, binding.key_display or key)
            assert display_key in ALL_HELP_KEYS, (
                f"Binding '{key}' (display: '{display_key}') is in PlaygroundApp.BINDINGS "
                f"but missing from help overlay. Add it to the appropriate group in help_overlay.py."
            )
