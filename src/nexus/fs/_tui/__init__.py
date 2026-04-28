"""nexus-fs playground — interactive TUI file browser.

Two-panel layout: mount list (left) + file browser (right) + status bar (bottom).
Keyboard-only navigation. Responsive: collapses mount panel at < 100 cols.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from contextlib import suppress
from typing import Any, cast

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Input, Static

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.fs._tui.auth_guidance import auth_guidance, format_runtime_error
from nexus.fs._tui.file_browser import FileBrowser
from nexus.fs._tui.file_preview import FilePreview
from nexus.fs._tui.help_overlay import HelpOverlay
from nexus.fs._tui.mount_panel import MountPanel

MIN_WIDTH = 80
MIN_HEIGHT = 24
MOUNT_PANEL_COLLAPSE_WIDTH = 100


class ContextualNexusFS:
    """Kernel wrapper that uses a caller-selected operation context."""

    def __init__(self, kernel: Any, *, user_id: str = "local") -> None:
        from nexus.contracts.types import OperationContext

        self._kernel = kernel
        self._ctx = OperationContext(
            user_id=user_id,
            groups=[],
            zone_id=ROOT_ZONE_ID,
            is_admin=True,
        )

    async def read(self, path: str) -> bytes:
        return cast(bytes, self._kernel.sys_read(path, context=self._ctx))

    def read_range(self, path: str, start: int, end: int) -> bytes:
        return cast(bytes, self._kernel.read_range(path, start, end, context=self._ctx))

    def write(self, path: str, content: bytes) -> dict[str, Any]:
        return cast(dict[str, Any], self._kernel.write(path, content, context=self._ctx))

    async def ls(
        self,
        path: str = "/",
        detail: bool = False,
        recursive: bool = False,
    ) -> list[str] | list[dict[str, Any]]:
        if not recursive and path != "/":
            backend_result = await self._list_backend_directory(path, detail=detail)
            if backend_result:
                return backend_result

        result = cast(
            list[str] | list[dict[str, Any]],
            self._kernel.sys_readdir(
                path,
                recursive=recursive,
                details=detail,
                context=self._ctx,
            ),
        )
        if result:
            return result
        if recursive:
            return result

        fallback = await self._list_backend_directory(path, detail=detail)
        return fallback if fallback is not None else result

    async def stat(self, path: str) -> dict[str, Any] | None:
        try:
            result = cast(
                "dict[str, Any] | None",
                self._kernel.sys_stat(path, context=self._ctx),
            )
            if result is not None:
                return result
        except Exception:
            pass
        return await self._stat_backend_path(path)

    async def mkdir(self, path: str, parents: bool = True) -> None:
        self._kernel.mkdir(path, parents=parents, exist_ok=True, context=self._ctx)

    async def rmdir(self, path: str, recursive: bool = False) -> None:
        self._kernel.rmdir(path, recursive=recursive, context=self._ctx)

    async def delete(self, path: str) -> None:
        self._kernel.sys_unlink(path, context=self._ctx)

    async def rename(self, old_path: str, new_path: str) -> None:
        self._kernel.sys_rename(old_path, new_path, context=self._ctx)

    def exists(self, path: str) -> bool:
        return cast(bool, self._kernel.access(path, context=self._ctx))

    def copy(self, src: str, dst: str) -> dict[str, Any]:
        result: dict[str, Any] = self._kernel.sys_copy(src, dst, context=self._ctx)
        return result

    def list_mounts(self) -> list[str]:
        # Use public syscall: readdir("/") returns top-level entries including mounts.
        _rk = getattr(self._kernel, "_kernel", None)
        if _rk is not None:
            entries = _rk.readdir("/", self._kernel._zone_id, True)
            mounts = [path for path, _etype in entries if path != "/"]
            return mounts or ["/"]
        result: list[str] = self._kernel._driver_coordinator.mount_points()
        return result

    async def close(self) -> None:
        return None

    async def _list_backend_directory(
        self,
        path: str,
        *,
        detail: bool,
    ) -> list[str] | list[dict[str, Any]] | None:
        """Fallback to backend.list_dir() when slim metadata has no children yet.

        After §12d Phase 2, Rust readdir merges backend entries for all
        backends.  This fallback covers the rare slim-mode case where
        metadata is not yet populated.
        """
        from datetime import UTC, datetime

        # Use Rust kernel sys_readdir_backend for listing (no route() needed).
        try:
            raw_entries = list(
                self._kernel._kernel.sys_readdir_backend(path, self._kernel._zone_id)
            )
        except Exception:
            return None
        if not raw_entries:
            return None

        now = datetime.now(UTC).isoformat()
        mount_root = path.rstrip("/") or "/"
        if detail:
            fallback_rows: list[dict[str, Any]] = []
            for entry in raw_entries:
                name = str(entry).rstrip("/")
                if not name:
                    continue
                is_dir = str(entry).endswith("/")
                full_path = f"{mount_root}/{name}" if mount_root != "/" else f"/{name}"
                fallback_rows.append(
                    {
                        "path": full_path,
                        "size": 4096 if is_dir else 0,
                        "is_directory": is_dir,
                        "content_id": None,
                        "mime_type": "inode/directory" if is_dir else "application/octet-stream",
                        "created_at": now,
                        "modified_at": now,
                        "version": 0,
                        "zone_id": "root",
                        "entry_type": 1 if is_dir else 0,
                    }
                )
            return fallback_rows

        fallback_paths: list[str] = []
        for entry in raw_entries:
            name = str(entry).rstrip("/")
            if not name:
                continue
            full_path = f"{mount_root}/{name}" if mount_root != "/" else f"/{name}"
            fallback_paths.append(full_path)
        return fallback_paths

    async def _stat_backend_path(self, path: str) -> dict[str, Any] | None:
        """Fallback stat for connector-backed entries not materialized in metadata."""
        normalized = path.rstrip("/") or "/"
        if normalized == "/":
            return None

        parent = normalized.rsplit("/", 1)[0] or "/"
        entries = await self._list_backend_directory(parent, detail=True)
        if not isinstance(entries, list):
            return None

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("path")) == normalized:
                return entry
        return None


def _highlight_match(text: str, query_lower: str) -> str:
    """Highlight the first occurrence of query in text with Rich markup.

    Case-insensitive match, preserves original casing in output.
    """
    idx = text.lower().find(query_lower)
    if idx == -1:
        return text
    end = idx + len(query_lower)
    return f"{text[:idx]}[bold yellow]{text[idx:end]}[/bold yellow]{text[end:]}"


class PlaygroundApp(App[None]):
    """Interactive TUI file browser for nexus-fs mounts.

    Args:
        uris: Backend URIs to mount (e.g., "s3://bucket", "local://./data").
            If empty, auto-discovers from NEXUS_FS_STATE_DIR.
    """

    TITLE = "nexus-fs playground"

    CSS = """
    #main-area {
        height: 1fr;
    }
    #playground-banner {
        width: 100%;
        height: auto;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $surface;
        padding: 0 1;
        color: $text-muted;
    }
    #search-input, #crud-input {
        width: 100%;
        display: none;
    }
    #command-bar {
        width: 100%;
        display: none;
        background: $surface;
        padding: 0 1;
    }
    #empty-state {
        width: 100%;
        content-align: center middle;
        padding: 4 2;
        color: $text-muted;
    }
    #connector-picker {
        width: 1fr;
        height: 1fr;
    }
    #picker-layout {
        width: 1fr;
        height: 1fr;
    }
    #picker-help {
        height: auto;
        padding: 0 1 1 1;
        color: $text-muted;
    }
    #picker-input {
        width: 100%;
        display: none;
    }
    #too-small-message {
        width: 100%;
        content-align: center middle;
        display: none;
    }
    """

    BINDINGS = [
        Binding("q", "request_quit", "Quit", priority=True),
        Binding("question_mark", "show_help", "Help", key_display="?", priority=True),
        Binding("b", "go_back", "Back"),
        Binding("/", "toggle_search", "Search", key_display="/"),
        Binding(":", "toggle_command", "Command", key_display=":", priority=True),
        Binding("backspace", "command_backspace", "", show=False, priority=True),
        Binding("a", "show_connector_picker", "Add Mount"),
        Binding("c", "copy_path", "Copy path"),
        Binding("p", "preview_file", "Preview"),
        Binding("m", "focus_mount_panel", "Mounts"),
        Binding("n", "new_file", "New file"),
        Binding("N", "new_dir", "New dir", key_display="N"),
        Binding("d", "delete_selected", "Delete"),
        Binding("r", "rename_selected", "Rename"),
        Binding("u", "unmount_selected_mount", "Unmount"),
    ]

    show_mount_panel: reactive[bool] = reactive(True)
    search_visible: reactive[bool] = reactive(False)
    command_visible: reactive[bool] = reactive(False)
    command_buffer: reactive[str] = reactive("")
    picker_visible: reactive[bool] = reactive(False)
    picker_input_visible: reactive[bool] = reactive(False)
    _crud_mode: str = ""  # "", "new_file", "new_dir", "rename", "delete_confirm"

    def __init__(self, uris: tuple[str, ...] = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._uris = uris
        self._fs: Any = None
        self._mount_points: list[str] = []
        self._current_path: str = "/"
        self._crud_rename_source: str = ""
        self._restored_mounts = False
        self._picker_title = "Supported connectors"
        self._picker_pending_uri: str | None = None
        self._mount_panel_auto_collapsed = False

    async def on_mount(self) -> None:
        """Initialize filesystem and load mounts."""
        fs = await self._resolve_filesystem()

        if fs is None:
            return

        self._fs = fs
        self._mount_points = fs.list_mounts()

        if not self._mount_points:
            await self._show_connector_picker("No mounts configured")
            return

        # Build the UI
        await self._build_browser_ui()

    async def _resolve_filesystem(self) -> Any:
        """Mount backends from URIs using direct or generic adapters."""
        if self._uris:
            try:
                return await self._build_filesystem(self._uris)
            except Exception as exc:
                empty = self.query_one("#empty-state", Static)
                empty.update(f"[red]Mount failed:[/red] {exc}")
                return None

        # No URIs — auto-discover from mounts.json in state dir
        from nexus.fs._paths import build_mount_args, load_persisted_mounts

        entries = load_persisted_mounts()
        if entries:
            try:
                saved_uris, overrides = build_mount_args(entries)
                self._restored_mounts = True
                self._persisted_entries = entries  # preserve for _persist_mounts
                return await self._build_filesystem(
                    tuple(saved_uris), mount_overrides=overrides or None
                )
            except Exception:
                pass  # Fall through to empty state

        await self._show_connector_picker("No mounts found")
        return None

    def _build_direct_fs(self, uris: tuple[str, ...]) -> Any:
        """Build direct filesystem adapters for local and S3 URIs."""
        from pathlib import Path as _Path

        from nexus.fs._tui.direct_fs import LocalDirectFS, MultiDirectFS, S3DirectFS

        backends: list[Any] = []
        for uri in uris:
            if uri.startswith("local://"):
                raw = uri.removeprefix("local://")
                root = _Path(raw).expanduser().resolve()
                if not root.exists():
                    root.mkdir(parents=True, exist_ok=True)
                name = root.name or "local"
                backends.append(LocalDirectFS(root, f"/local/{name}"))

            elif uri.startswith("s3://"):
                raw = uri.removeprefix("s3://")
                parts = raw.split("/", 1)
                bucket = parts[0]
                prefix = parts[1] if len(parts) > 1 else ""
                backends.append(S3DirectFS(bucket, prefix, f"/s3/{bucket}"))

            else:
                raise ValueError(
                    f"Unsupported direct scheme in '{uri}'. Use the generic mount path for connector-backed URIs."
                )

        if len(backends) == 1:
            return backends[0]
        return MultiDirectFS(backends)

    async def _build_filesystem(
        self,
        uris: tuple[str, ...],
        mount_overrides: dict[str, str] | None = None,
    ) -> Any:
        """Build a hybrid filesystem for playground mounts."""
        from nexus.fs import mount as mount_fs
        from nexus.fs._tui.direct_fs import MultiDirectFS

        overrides = mount_overrides or {}
        direct_uris = tuple(uri for uri in uris if uri.startswith(("local://", "s3://")))
        generic_uris = tuple(uri for uri in uris if uri not in direct_uris)
        backends: list[Any] = []

        if direct_uris:
            backends.append(self._build_direct_fs(direct_uris))

        for uri in generic_uris:
            at = overrides.get(uri)
            kernel = await mount_fs(uri, at=at)
            backends.append(
                ContextualNexusFS(
                    kernel,
                    user_id=await self._resolve_mount_user_id(uri),
                )
            )

        if not backends:
            raise ValueError("No valid mount URIs provided.")
        if len(backends) == 1:
            return backends[0]
        return MultiDirectFS(backends)

    async def _build_browser_ui(self) -> None:
        """Build the file browser UI after mounts are resolved."""
        main = self.query_one("#main-area", Horizontal)

        # Mount panel
        mount_panel = MountPanel(self._fs, self._mount_points, id="mount-panel")
        await main.mount(mount_panel)
        mount_panel.display = self.show_mount_panel

        # File browser
        browser = FileBrowser(self._fs, id="file-browser")
        await main.mount(browser)

        # File preview
        preview = FilePreview(self._fs, id="file-preview")
        preview.display = False
        await main.mount(preview)

        # Remove empty state
        empty = self.query_one("#empty-state", Static)
        empty.display = False
        self.picker_visible = False
        self.picker_input_visible = False
        self._picker_pending_uri = None
        self._update_banner()

        # Load first mount and focus the file table
        if self._mount_points:
            await browser.load_directory(self._mount_points[0])
            self._current_path = self._mount_points[0]
            self._update_status_bar(announce=False)
            browser.query_one("#file-table").focus()

    async def _show_connector_picker(self, title: str = "Supported connectors") -> None:
        """Render the interactive connector picker in the main area."""
        main = self.query_one("#main-area", Horizontal)
        for child in list(main.children):
            await child.remove()

        empty = self.query_one("#empty-state", Static)
        empty.display = True
        empty.update(
            f"[bold]{title}[/bold]\n"
            "[dim]Browse connectors below, press Enter to continue, and complete any required values directly in the TUI.[/dim]"
        )

        layout = Vertical(id="picker-layout")
        await main.mount(layout)

        picker_help = Static("", id="picker-help")
        await layout.mount(picker_help)

        picker = DataTable(id="connector-picker")
        picker.cursor_type = "row"
        picker.add_columns("Connector", "Mode")
        for uri, mode in self._supported_connector_rows():
            picker.add_row(uri, mode)
        await layout.mount(picker)

        picker_input = self.query_one("#picker-input", Input)
        picker_input.display = False
        picker_input.value = ""
        picker_input.placeholder = ""

        self.picker_visible = True
        self.picker_input_visible = False
        self._picker_title = title
        self._picker_pending_uri = None
        if picker.row_count:
            picker.move_cursor(row=0, column=0)
            self._update_picker_help_for_row(0)
        picker.focus()
        self._update_banner()
        self.query_one("#status-bar", Static).update(
            "[dim]Mount wizard | arrows to browse | Enter to continue | Esc back | a reopen later[/dim]"
        )

    async def _mount_selected_picker_uri(self) -> bool:
        """Mount the currently highlighted connector-picker row."""
        if not self.picker_visible:
            return False
        try:
            picker = self.query_one("#connector-picker", DataTable)
        except Exception:
            return False
        if self.focused is not picker or picker.row_count <= 0:
            return False
        try:
            cursor_row = max(0, picker.cursor_row)
            uri = str(picker.get_row_at(cursor_row)[0])
        except Exception:
            return False
        await self._handle_picker_uri(uri)
        return True

    async def _handle_picker_uri(self, uri: str) -> None:
        """Either mount directly or prompt for URI details in the picker."""
        if self._uri_requires_customization(uri):
            self._show_picker_input(uri)
            return
        await self._mount_uri(uri)

    def _uri_requires_customization(self, uri: str) -> bool:
        """Whether a picker row needs user input before mounting."""
        return "<" in uri or uri == "local://./data"

    def _picker_input_defaults(self, uri: str) -> tuple[str, str]:
        """Prefill and placeholder for a wizard URI input."""
        cwd = os.getcwd()
        if uri == "local://./data":
            return (f"local://{cwd}/data", "local:///absolute/path")
        if uri.startswith("s3://"):
            return ("s3://", "s3://bucket[/prefix]")
        if uri.startswith("gcs://"):
            return ("gcs://", "gcs://project/bucket[/prefix]")
        return (uri, uri)

    def _show_picker_input(self, uri: str) -> None:
        """Switch the mount wizard into URI input mode."""
        picker_input = self.query_one("#picker-input", Input)
        value, placeholder = self._picker_input_defaults(uri)
        picker_input.value = value
        picker_input.placeholder = placeholder
        self._picker_pending_uri = uri
        self.picker_input_visible = True
        self._update_picker_help(uri=uri, awaiting_input=True)
        picker_input.focus()

    def _update_banner(self) -> None:
        """Render a visible top-of-screen summary of available actions."""
        try:
            banner = self.query_one("#playground-banner", Static)
        except Exception:
            return
        if self.picker_visible:
            banner.update(
                "[bold]Add Mount[/bold] Browse supported connectors, press Enter to continue, "
                "and finish setup in the TUI. [dim]Keys:[/dim] arrows browse  Enter continue  Esc back"
            )
            return

        restored = ""
        if self._restored_mounts and self._mount_points:
            restored = f"[yellow]Restored {len(self._mount_points)} mount(s) from the previous session.[/yellow]  "
        mount_hint = ""
        if self._mount_panel_auto_collapsed and not self.show_mount_panel:
            mount_hint = "[bold]Mounts:[/bold] `m` show mounts  "
        banner.update(
            f"{restored}[bold]Add Mount:[/bold] `a`  "
            f"{mount_hint}"
            "[bold]Mounts:[/bold] `m` focus  `u` unmount  "
            "[bold]Open:[/bold] `Enter` selected folder/file  "
            "[bold]Ops:[/bold] `n` file  `N` dir  `r` rename  `d` delete  `p` preview"
        )

    def _update_picker_help_for_row(self, row_index: int) -> None:
        """Refresh picker help for the highlighted connector row."""
        try:
            picker = self.query_one("#connector-picker", DataTable)
            uri = str(picker.get_row_at(row_index)[0])
        except Exception:
            return
        self._update_picker_help(uri=uri, awaiting_input=self.picker_input_visible)

    def _update_picker_help(self, *, uri: str, awaiting_input: bool) -> None:
        """Render contextual guidance for the selected picker row."""
        help_widget = self.query_one("#picker-help", Static)
        guidance: list[str] = []
        if uri.startswith("local://"):
            guidance.append("Local folder mount. Choose any absolute path on disk.")
            guidance.append(
                "After mounting you can create files with `n`, directories with `N`, and preview with `p`."
            )
        elif uri.startswith("s3://"):
            guidance.append("S3 mount. Enter `s3://bucket` or `s3://bucket/prefix`.")
            guidance.append(
                "If AWS credentials are available, concrete `s3://...` bucket rows appear in this picker and can be mounted directly."
            )
            guidance.append(self._auth_guidance("s3"))
        elif uri.startswith("gcs://"):
            guidance.append(
                "GCS mount. Enter `gcs://project/bucket` or `gcs://project/bucket/prefix`."
            )
            guidance.append(self._auth_guidance("gcs"))
        elif uri.startswith(("gws://", "gdrive://", "gmail://", "calendar://")):
            guidance.append(f"Google mount target: `{uri}`.")
            guidance.append(self._auth_guidance("gws"))
        else:
            guidance.append(f"Mount target: `{uri}`.")
        if awaiting_input:
            guidance.append(
                "Edit the URI below and press Enter to mount, or Esc to return to the list."
            )
        else:
            guidance.append("Press Enter to mount this target.")
        help_widget.update("\n".join(guidance))

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="playground-banner")
        yield Static("", id="too-small-message")
        empty = Static(
            "[dim]Loading mounts…[/dim]",
            id="empty-state",
        )
        empty.can_focus = False
        yield empty
        yield Horizontal(id="main-area")
        yield Input(
            placeholder="Search files… (Enter to search, Escape to cancel)",
            id="search-input",
        )
        yield Input(placeholder="", id="picker-input")
        yield Static("", id="command-bar")
        yield Input(placeholder="", id="crud-input")
        yield Static("", id="status-bar")
        yield Footer()

    def _update_status_bar(self, announce: bool = True) -> None:
        """Update the bottom status bar and optionally announce for screen readers."""
        bar = self.query_one("#status-bar", Static)
        mount_count = len(self._mount_points)

        try:
            browser = self.query_one("#file-browser", FileBrowser)
            file_count = browser.entry_count
        except Exception:
            file_count = 0

        status_text = f"{mount_count} mount(s) | {file_count} entries | {self._current_path}"
        bar.update(f"[dim]{status_text}[/dim]")

        # Screen reader announcement on navigation
        if announce:
            self.notify(
                f"{self._current_path} — {file_count} entries",
                timeout=1,
                severity="information",
            )

    # -- Event handlers --

    async def on_mount_panel_mount_selected(self, event: MountPanel.MountSelected) -> None:
        """Handle mount selection from the mount panel."""
        try:
            browser = self.query_one("#file-browser", FileBrowser)
            await browser.load_directory(event.mount_point)
            self._current_path = event.mount_point
            self._update_status_bar()
        except Exception:
            pass

    async def on_file_browser_directory_changed(self, event: FileBrowser.DirectoryChanged) -> None:
        """Navigate into a directory."""
        try:
            browser = self.query_one("#file-browser", FileBrowser)
            await browser.load_directory(event.path)
            self._current_path = event.path
            self._update_status_bar()
        except Exception:
            pass

    async def on_file_browser_file_selected(self, event: FileBrowser.FileSelected) -> None:
        """Show file preview when a file is selected."""
        try:
            preview = self.query_one("#file-preview", FilePreview)
            preview.display = True
            await preview.show_preview(event.path)
        except Exception:
            pass

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle connector picker selection."""
        if event.data_table.id != "connector-picker":
            return
        try:
            uri = str(event.data_table.get_row_at(event.cursor_row)[0])
        except Exception:
            return
        await self._handle_picker_uri(uri)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Refresh picker guidance while the selection changes."""
        if event.data_table.id != "connector-picker":
            return
        self._update_picker_help_for_row(event.cursor_row)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Route input submissions to search or CRUD handler."""
        if event.input.id == "picker-input":
            value = event.value.strip()
            if value:
                await self._mount_uri(value)
                return
            self.notify("Mount URI is required.", severity="warning", timeout=4)
            return

        # CRUD input
        if event.input.id == "crud-input":
            await self._on_crud_input_submitted(event.value.strip())
            return

        # Search input
        query = event.value.strip()
        if not query:
            self.search_visible = False
            return

        self.search_visible = False
        event.input.value = ""

        if not self._fs or not self._mount_points:
            return

        try:
            browser = self.query_one("#file-browser", FileBrowser)
        except Exception:
            return

        # Search across all mounts concurrently
        all_matches: list[dict] = []
        succeeded: list[str] = []
        failed: list[str] = []

        async def _search_mount(mount: str) -> list[dict]:
            entries = self._fs.ls(mount, detail=True, recursive=True)
            return [e for e in entries if query.lower() in e.get("path", "").lower()]

        tasks = {mp: _search_mount(mp) for mp in self._mount_points}
        for mp, coro in tasks.items():
            try:
                results = await asyncio.wait_for(coro, timeout=3.0)
                all_matches.extend(results)
                succeeded.append(mp)
            except Exception:
                failed.append(mp)

        # Update browser with results
        browser._entries = all_matches[:500]
        browser._total_count = len(all_matches)
        table = browser.query_one("#file-table")
        table.clear()
        from nexus.fs._tui.file_browser import _format_modified, _format_size

        query_lower = query.lower()
        for entry in browser._entries:
            # Search results show full path (not just basename) so
            # cross-mount and duplicate-filename results are unambiguous.
            full_path = entry.get("path", "").rstrip("/")
            is_dir = entry.get("is_directory", False)

            # Highlight matching text in the full path
            display_path = _highlight_match(full_path, query_lower)

            if is_dir:
                display_path = f"[bold cyan]{display_path}/[/bold cyan]"
            size = "—" if is_dir else _format_size(entry.get("size", 0))
            modified = _format_modified(entry.get("modified_at"))
            table.add_row(display_path, size, modified)

        # Build status with partial backend indicator
        overflow = browser.query_one("#overflow", Static)
        parts: list[str] = []
        if all_matches:
            count_str = f"{len(all_matches)} result(s) for '{query}'"
            if len(all_matches) > 500:
                count_str = f"showing 500 of {len(all_matches)} results for '{query}'"
            parts.append(f"[dim]{count_str}[/dim]")
        else:
            parts.append(f"[dim]No results for '{query}'[/dim]")

        # Show green/red dots for backend status
        if len(self._mount_points) > 1:
            backend_status = " ".join(
                f"[green]●[/green]{mp.split('/')[-1]}"
                if mp in succeeded
                else f"[red]●[/red]{mp.split('/')[-1]}"
                for mp in self._mount_points
            )
            parts.append(backend_status)

        overflow.update("  ".join(parts))

    # -- CRUD input handler --

    async def _on_crud_input_submitted(self, value: str) -> None:
        """Handle CRUD input submissions (new file, new dir, rename)."""
        crud_input = self.query_one("#crud-input", Input)
        crud_input.display = False
        crud_input.value = ""
        mode = self._crud_mode
        self._crud_mode = ""

        if not value or not self._fs:
            self._refocus_table()
            return

        try:
            browser = self.query_one("#file-browser", FileBrowser)
        except Exception:
            return

        if mode == "new_file":
            path = f"{self._current_path.rstrip('/')}/{value}"
            try:
                self._fs.write(path, b"")
                self.notify(f"Created: {value}", timeout=2)
                await browser.load_directory(self._current_path)
                self._update_status_bar()
            except Exception as exc:
                self.notify(f"Create failed: {exc}", severity="error", timeout=3)

        elif mode == "new_dir":
            path = f"{self._current_path.rstrip('/')}/{value}"
            try:
                self._fs.mkdir(path)
                self.notify(f"Created directory: {value}", timeout=2)
                await browser.load_directory(self._current_path)
                self._update_status_bar()
            except Exception as exc:
                self.notify(f"Mkdir failed: {exc}", severity="error", timeout=3)

        elif mode == "rename":
            old_path = self._crud_rename_source
            new_name = value
            parent = old_path.rstrip("/").rsplit("/", 1)[0]
            new_path = f"{parent}/{new_name}"
            try:
                self._fs.rename(old_path, new_path)
                self.notify(f"Renamed → {new_name}", timeout=2)
                await browser.load_directory(self._current_path)
                self._update_status_bar()
            except Exception as exc:
                self.notify(f"Rename failed: {exc}", severity="error", timeout=3)

        self._refocus_table()

    async def _on_command_input_submitted(self, value: str) -> None:
        """Handle command input submissions."""
        self.command_visible = False
        self.command_buffer = ""
        self.command_visible = False

        if not value:
            self._refocus_table()
            return

        normalized = value[1:] if value.startswith("/") else value
        parts = normalized.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if command == "mount":
            if not arg:
                await self._show_connector_picker("Add a mount")
                return
            await self._mount_uri(arg)
            self._refocus_table()
            return

        if command == "touch":
            if not arg:
                self.notify("Usage: /touch <name>", severity="warning", timeout=4)
                self._refocus_table()
                return
            await self._touch_path(arg)
            self._refocus_table()
            return

        if command == "rm":
            if not arg:
                self.notify("Usage: /rm <name>", severity="warning", timeout=4)
                self._refocus_table()
                return
            await self._delete_path(arg)
            self._refocus_table()
            return

        if command == "auth":
            if arg.lower().startswith("test "):
                service = arg[5:].strip().lower()
                await self._test_auth(service)
                self._refocus_table()
                return
            service = arg.lower() if arg else "s3"
            self.notify(self._auth_guidance(service), severity="information", timeout=6)
            self._refocus_table()
            return

        if command == "connectors":
            self.notify(self._connector_summary(), severity="information", timeout=8)
            self._refocus_table()
            return

        self.notify(f"Unknown command: {value}", severity="warning", timeout=4)
        self._refocus_table()

    async def _mount_uri(self, uri: str) -> None:
        """Mount a new backend URI into the playground."""
        error = self._validate_mount_uri(uri)
        if error:
            self.notify(error, severity="warning", timeout=5)
            return
        next_uris = (*self._uris, uri)
        try:
            fs = await self._build_filesystem(next_uris)
        except Exception as exc:
            self.notify(self._mount_error_message(uri, exc), severity="error", timeout=6)
            return

        self._uris = next_uris
        self._fs = fs
        self._mount_points = fs.list_mounts()
        self._restored_mounts = False
        self._persist_mounts()
        await self._reset_browser_ui()
        self.notify(f"Mounted {uri}", timeout=3)

    def _validate_mount_uri(self, uri: str) -> str | None:
        """Return a user-facing error if the requested mount URI is incomplete."""
        value = uri.strip()
        if not value:
            return "Mount URI is required."
        if value in {"s3://", "s3:///"}:
            return "S3 mounts require a bucket, for example `s3://my-bucket`."
        if value in {"gcs://", "gcs:///"}:
            return "GCS mounts require a bucket, for example `gcs://project/my-bucket`."
        return None

    async def _touch_path(self, name: str) -> None:
        """Create an empty file in the current directory."""
        if not self._fs:
            return
        path = self._resolve_command_path(name)
        try:
            self._fs.write(path, b"")
            browser = self.query_one("#file-browser", FileBrowser)
            await browser.load_directory(self._current_path)
            self._update_status_bar()
            self.notify(f"Created: {path}", timeout=3)
        except Exception as exc:
            self.notify(f"Create failed: {exc}", severity="error", timeout=4)

    async def _delete_path(self, name: str) -> None:
        """Delete a file in the current directory."""
        if not self._fs:
            return
        path = self._resolve_command_path(name)
        try:
            self._fs.delete(path)
            browser = self.query_one("#file-browser", FileBrowser)
            await browser.load_directory(self._current_path)
            self._update_status_bar()
            self.notify(f"Deleted: {path}", timeout=3)
        except Exception as exc:
            self.notify(f"Delete failed: {exc}", severity="error", timeout=4)

    async def _test_auth(self, service: str) -> None:
        """Validate auth for a service using the unified auth layer when possible."""
        if not service:
            self.notify("Usage: /auth test <service>", severity="warning", timeout=4)
            return
        if service == "local":
            self.notify("local: no auth required", severity="information", timeout=4)
            return

        try:
            from nexus.fs._oauth_support import get_token_manager

            oauth_module = importlib.import_module("nexus.bricks.auth.oauth.credential_service")
            unified_module = importlib.import_module("nexus.bricks.auth.unified_service")
            oauth_service = oauth_module.OAuthCredentialService(token_manager=get_token_manager())
            auth_service = unified_module.UnifiedAuthService(oauth_service=oauth_service)
            result = await auth_service.test_service(service)
        except Exception as exc:
            self.notify(f"{service}: auth test failed to run: {exc}", severity="error", timeout=6)
            return

        if result.get("success"):
            self.notify(f"{service}: {result.get('message')}", severity="information", timeout=5)
            return

        self.notify(f"{service}: {result.get('message')}", severity="warning", timeout=7)

    def _resolve_command_path(self, name: str) -> str:
        """Resolve a command argument against the current directory."""
        if name.startswith("/"):
            return name
        base = self._current_path.rstrip("/")
        if not base:
            return f"/{name}"
        return f"{base}/{name}"

    async def _reset_browser_ui(self) -> None:
        """Rebuild mount panel, browser, and preview from current mounts."""
        try:
            main = self.query_one("#main-area", Horizontal)
            for child in list(main.children):
                await child.remove()
        except Exception:
            return

        empty = self.query_one("#empty-state", Static)
        empty.display = False
        await self._build_browser_ui()

    def _persist_mounts(self) -> None:
        """Persist current mount URIs for the next playground launch.

        Preserves ``at`` metadata from previously persisted entries so that
        custom mount points set via ``nexus-fs mount --at`` survive.
        """
        from nexus.fs._paths import save_persisted_mounts

        # Build an index of at-values from restored entries
        prev = getattr(self, "_persisted_entries", None) or []
        at_by_uri = {e["uri"]: e.get("at") for e in prev}

        entries = [{"uri": uri, "at": at_by_uri.get(uri)} for uri in self._uris]
        save_persisted_mounts(entries, merge=False)

    def _selected_mount_point(self) -> str | None:
        """Return the currently selected mount point from the mount panel."""
        try:
            panel = self.query_one("#mount-panel", MountPanel)
            return cast(str | None, panel.selected_mount)
        except Exception:
            return self._mount_points[0] if self._mount_points else None

    def _uri_mount_point(self, uri: str) -> str:
        """Compute the mount point produced by a playground URI."""
        from pathlib import Path as _Path

        from nexus.fs._uri import derive_mount_point, parse_uri

        if uri.startswith("local://"):
            raw = uri.removeprefix("local://")
            root = _Path(raw).expanduser().resolve()
            name = root.name or "local"
            return f"/local/{name}"
        if uri.startswith("s3://"):
            raw = uri.removeprefix("s3://")
            bucket = raw.split("/", 1)[0]
            return f"/s3/{bucket}"
        spec = parse_uri(uri)
        return derive_mount_point(spec)

    def _mount_error_message(self, uri: str, exc: Exception) -> str:
        """Render actionable mount failures."""
        return format_runtime_error(uri, exc)

    def _auth_guidance(self, service: str) -> str:
        """Return concise auth guidance for playground-supported backends."""
        return auth_guidance(service)

    def _connector_summary(self) -> str:
        """Concise supported connector summary for the command bar."""
        items = self._supported_connector_rows()
        return " | ".join(f"{name}:{mode}" for name, mode in items[:8])

    def _supported_connectors_text(self, title: str) -> str:
        """Empty-state help for supported playground connectors."""
        rows = self._supported_connector_rows()
        rendered = "\n".join(
            f"{idx}. {name} [{mode}]" for idx, (name, mode) in enumerate(rows, start=1)
        )
        return (
            f"[bold]{title}[/bold]\n\n"
            "[bold]Supported playground connectors[/bold]\n"
            f"{rendered}\n\n"
            "[bold]Auth guidance[/bold]\n"
            "1. /auth s3\n"
            "2. /auth gws\n\n"
            "[bold]Examples[/bold]\n"
            "1. : then /mount local://./data\n"
            "2. : then /mount s3://bucket\n"
            "3. : then /mount gws://drive\n"
            "4. : then /connectors\n"
            "5. : then /auth test s3"
        )

    def _supported_connector_rows(self) -> list[tuple[str, str]]:
        """Dynamically render concrete playground connector targets."""
        from nexus.backends import _register_optional_backends
        from nexus.backends.base.registry import ConnectorRegistry

        _register_optional_backends()

        rows: list[tuple[str, str]] = [("local://./data", "mountable")]
        rows.extend(self._discovered_s3_bucket_rows())
        rows.extend(
            [
                ("s3://<enter bucket manually>", "mountable auth:s3 manual"),
                ("gcs://project/bucket", "mountable auth:gcs"),
            ]
        )
        for info in ConnectorRegistry.list_all():
            uri = self._connector_uri_example(info.name)
            if uri is None:
                continue
            auth_service = self._connector_auth_service(info.name, info.service_name)
            mode = "mountable" if auth_service is None else f"mountable auth:{auth_service}"
            rows.append((uri, mode))
        deduped: list[tuple[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            if row[0] in seen:
                continue
            seen.add(row[0])
            deduped.append(row)
        return deduped

    def _discovered_s3_bucket_rows(self) -> list[tuple[str, str]]:
        """Return concrete S3 buckets when AWS credentials can enumerate them."""
        try:
            import boto3
            from botocore.config import Config
        except Exception:
            return []

        try:
            client = boto3.client("s3", config=Config(connect_timeout=2, read_timeout=3))
            response = client.list_buckets()
        except Exception:
            return []

        buckets = response.get("Buckets") or []
        rows: list[tuple[str, str]] = []
        for bucket in buckets:
            name = str(bucket.get("Name") or "").strip()
            if not name:
                continue
            rows.append((f"s3://{name}", "mountable auth:s3 discovered"))
        return rows

    def _connector_uri_example(self, connector_name: str) -> str | None:
        """Concrete URI example for a registered connector."""
        if connector_name in {
            "cas_gcs",
            "cas_local",
            "local_connector",
            "path_gcs",
            "path_local",
            "path_s3",
        }:
            return None
        if connector_name == "gdrive_connector":
            return "gdrive://root"
        if connector_name == "gmail_connector":
            return "gmail://inbox"
        if connector_name == "calendar_connector":
            return "gws://calendar"
        if connector_name == "slack_connector":
            return "slack://workspace"
        if connector_name == "x_connector":
            return "x://timeline"
        if connector_name == "hn_connector":
            return "hn://top"
        # ``github_connector`` is the canonical GitHub registry name
        # (#3728).  ``gws_github`` is a deprecated alias kept for
        # backward compatibility of persisted mounts — both must
        # advertise the same ``github://`` URI example so the TUI and
        # playground don't show the broken ``gws://github`` path.
        if connector_name in {"github_connector", "gws_github"}:
            return "github://me"
        if connector_name.startswith("gws_"):
            return f"gws://{connector_name.removeprefix('gws_')}"
        return None

    def _connector_auth_service(self, connector_name: str, service_name: str | None) -> str | None:
        """Map connector targets to the auth flow users should follow."""
        if connector_name in {"github_connector", "gws_github"}:
            return "github"
        if connector_name.startswith("gws_"):
            return "gws"
        if service_name in {"google-drive", "gmail", "google-calendar", "slack", "x", "gcs"}:
            return service_name
        return None

    async def _resolve_mount_user_id(self, uri: str) -> str:
        """Choose a user identity for connector-backed mounts."""
        explicit = os.getenv("NEXUS_FS_USER_EMAIL")
        if explicit:
            return explicit

        if not uri.startswith(
            (
                "gws://",
                "gdrive://",
                "gmail://",
                "calendar://",
                "slack://",
                "x://",
                "github://",
            )
        ):
            return "local"

        if uri.startswith(("gws://", "gdrive://", "gmail://", "calendar://")):
            providers = {"google"}
        elif uri.startswith("slack://"):
            providers = {"slack"}
        elif uri.startswith("github://"):
            providers = {"github"}
        else:
            providers = {"x", "twitter"}

        try:
            from nexus.fs._oauth_support import get_token_manager

            oauth_module = importlib.import_module("nexus.bricks.auth.oauth.credential_service")
            oauth_service = oauth_module.OAuthCredentialService(token_manager=get_token_manager())
            creds = await oauth_service.list_credentials()
            emails = sorted(
                {
                    str(cred.get("user_email"))
                    for cred in creds
                    if cred.get("provider") in providers and cred.get("user_email")
                }
            )
            if len(emails) == 1:
                return emails[0]
        except Exception:
            pass

        try:
            from types import SimpleNamespace

            from nexus.fs._backend_factory import _infer_connector_user_email
        except Exception:
            return "local"

        scheme = uri.split("://", 1)[0].lower()
        service_name = None
        if scheme == "gws":
            service_name = "gws"
        elif scheme == "gdrive":
            service_name = "google-drive"
        elif scheme == "gmail":
            service_name = "gmail"
        elif scheme == "calendar":
            service_name = "google-calendar"
        elif scheme == "slack":
            service_name = "slack"
        elif scheme == "x":
            service_name = "x"
        elif scheme == "github":
            service_name = "github"

        inferred = _infer_connector_user_email(
            scheme=scheme,
            info=SimpleNamespace(service_name=service_name),
        )
        return inferred or "local"

    def _refocus_table(self) -> None:
        """Return focus to the file table after CRUD operations."""
        import contextlib

        with contextlib.suppress(Exception):
            if self.picker_visible:
                self.query_one("#connector-picker", DataTable).focus()
                return
            self.query_one("#file-table").focus()

    def _show_crud_input(self, mode: str, placeholder: str, prefill: str = "") -> None:
        """Show the CRUD input with a given mode and placeholder."""
        self._crud_mode = mode
        crud_input = self.query_one("#crud-input", Input)
        crud_input.placeholder = placeholder
        crud_input.value = prefill
        crud_input.display = True
        crud_input.focus()

    # -- Actions --

    def action_request_quit(self) -> None:
        """Quit immediately without confirmation dialog."""
        self.exit()

    def action_show_help(self) -> None:
        """Show the keybinding help overlay."""
        if isinstance(self.focused, Input):
            return
        self.push_screen(HelpOverlay())

    def action_new_file(self) -> None:
        """Create a new empty file in the current directory."""
        self._show_crud_input("new_file", "New file name (Enter to create, Escape to cancel)")

    def action_new_dir(self) -> None:
        """Create a new directory in the current directory."""
        self._show_crud_input("new_dir", "New directory name (Enter to create, Escape to cancel)")

    async def action_delete_selected(self) -> None:
        """Delete the currently selected file or directory."""
        if not self._fs:
            return
        try:
            browser = self.query_one("#file-browser", FileBrowser)
            idx = browser.query_one("#file-table").cursor_row
            if idx >= len(browser._entries):
                return
            entry = browser._entries[idx]
            path = entry.get("path", "")
            name = path.rstrip("/").rsplit("/", 1)[-1]
            is_dir = entry.get("is_directory", False)

            if is_dir:
                self._fs.rmdir(path, recursive=True)
                self.notify(f"Deleted directory: {name}", timeout=2)
            else:
                self._fs.delete(path)
                self.notify(f"Deleted: {name}", timeout=2)

            await browser.load_directory(self._current_path)
            self._update_status_bar()
        except Exception as exc:
            self.notify(f"Delete failed: {exc}", severity="error", timeout=3)

    def action_rename_selected(self) -> None:
        """Rename the currently selected file or directory."""
        try:
            browser = self.query_one("#file-browser", FileBrowser)
            idx = browser.query_one("#file-table").cursor_row
            if idx >= len(browser._entries):
                return
            entry = browser._entries[idx]
            path = entry.get("path", "")
            name = path.rstrip("/").rsplit("/", 1)[-1]
            self._crud_rename_source = path
            self._show_crud_input("rename", "New name (Enter to rename, Escape to cancel)", name)
        except Exception:
            pass

    def action_go_back(self) -> None:
        """Navigate back."""
        if self.picker_visible and self._mount_points:
            self.call_later(self._reset_browser_ui)
            return
        try:
            browser = self.query_one("#file-browser", FileBrowser)
            browser.go_back()
        except Exception:
            pass

    def action_toggle_search(self) -> None:
        """Toggle search input."""
        self.search_visible = not self.search_visible

    def action_toggle_command(self) -> None:
        """Toggle command input."""
        self.command_visible = not self.command_visible

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:  # noqa: ARG002
        """Disable non-command actions when command mode is active."""
        if self.command_visible:
            return action in {
                "toggle_command",
                "submit_command",
                "command_backspace",
                "cancel_command",
            }
        return True

    async def action_show_connector_picker(self) -> None:
        """Open the interactive connector picker."""
        await self._show_connector_picker("Mount another connector")

    async def action_submit_command(self) -> None:
        """Submit the current command buffer."""
        if self.picker_input_visible:
            picker_input = self.query_one("#picker-input", Input)
            value = picker_input.value.strip()
            if value:
                await self._mount_uri(value)
            else:
                self.notify("Mount URI is required.", severity="warning", timeout=4)
            return
        if await self._mount_selected_picker_uri():
            return
        if not self.command_visible:
            return
        await self._on_command_input_submitted(self.command_buffer.strip())

    def action_command_backspace(self) -> None:
        """Delete the last command character."""
        if self.command_visible:
            self.command_buffer = self.command_buffer[:-1]

    def action_cancel_command(self) -> None:
        """Close the command bar and clear the buffer."""
        if not self.command_visible:
            return
        self.command_visible = False
        self.command_buffer = ""
        self._refocus_table()

    def watch_picker_input_visible(self, visible: bool) -> None:
        """Show or hide the mount wizard URI input."""
        try:
            picker_input = self.query_one("#picker-input", Input)
            picker_input.display = visible
            if visible:
                picker_input.focus()
        except Exception:
            pass

    def watch_search_visible(self, visible: bool) -> None:
        """Show/hide search input."""
        try:
            search = self.query_one("#search-input", Input)
            search.display = visible
            if visible:
                search.focus()
        except Exception:
            pass

    def watch_command_visible(self, visible: bool) -> None:
        """Show/hide command bar."""
        try:
            command = self.query_one("#command-bar", Static)
            command.display = visible
        except Exception:
            pass

    def watch_command_buffer(self, value: str) -> None:
        """Render the current command buffer."""
        try:
            command = self.query_one("#command-bar", Static)
            cursor = "█" if self.command_visible else ""
            command.update(f"> {value}{cursor}")
        except Exception:
            pass

    def action_copy_path(self) -> None:
        """Copy current file path to system clipboard."""
        try:
            browser = self.query_one("#file-browser", FileBrowser)
            path = browser.copy_current_path()
            if path:
                self.copy_to_clipboard(path)
                self.notify(f"Copied: {path}", timeout=2)
        except Exception:
            pass

    async def action_preview_file(self) -> None:
        """Preview the currently selected file (skip directories)."""
        try:
            browser = self.query_one("#file-browser", FileBrowser)
            idx = browser.query_one("#file-table").cursor_row
            if idx >= len(browser._entries):
                return
            entry = browser._entries[idx]
            if entry.get("is_directory", False):
                return
            path = entry.get("path")
            if path:
                preview = self.query_one("#file-preview", FilePreview)
                preview.display = True
                await preview.show_preview(path)
        except Exception:
            pass

    def action_toggle_mount_panel(self) -> None:
        """Toggle mount panel visibility."""
        self.show_mount_panel = not self.show_mount_panel

    def action_focus_mount_panel(self) -> None:
        """Focus the mount list for navigation and unmount actions."""
        try:
            panel = self.query_one("#mount-panel", MountPanel)
        except Exception:
            return
        if not self.show_mount_panel:
            self.show_mount_panel = True
        panel.focus()

    async def action_unmount_selected_mount(self) -> None:
        """Remove the currently selected mount from the playground session."""
        if not self._uris:
            return
        mount_point = self._selected_mount_point()
        if not mount_point:
            return
        remaining_uris = tuple(
            uri for uri in self._uris if self._uri_mount_point(uri) != mount_point
        )
        if len(remaining_uris) == len(self._uris):
            self.notify(
                f"Unmount failed: no mount found for {mount_point}", severity="warning", timeout=4
            )
            return

        self._uris = remaining_uris
        self._restored_mounts = False
        if not remaining_uris:
            self._fs = None
            self._mount_points = []
            self._persist_mounts()
            try:
                main = self.query_one("#main-area", Horizontal)
                for child in list(main.children):
                    await child.remove()
            except Exception:
                pass
            empty = self.query_one("#empty-state", Static)
            empty.display = True
            await self._show_connector_picker("No mounts configured")
            self.notify(f"Unmounted {mount_point}", timeout=3)
            return

        try:
            fs = await self._build_filesystem(remaining_uris)
        except Exception as exc:
            self.notify(f"Unmount failed: {exc}", severity="error", timeout=4)
            return

        self._fs = fs
        self._mount_points = fs.list_mounts()
        self._persist_mounts()
        await self._reset_browser_ui()
        self.notify(f"Unmounted {mount_point}", timeout=3)

    def watch_show_mount_panel(self, show: bool) -> None:
        """Show/hide mount panel."""
        try:
            panel = self.query_one("#mount-panel", MountPanel)
            panel.display = show
        except Exception:
            pass
        self._update_banner()

    def on_resize(self, event: Any) -> None:  # noqa: ARG002
        """Handle terminal resize for responsive layout."""
        width = self.size.width
        height = self.size.height

        # Graceful too-small message
        try:
            too_small = self.query_one("#too-small-message", Static)
        except Exception:
            return

        if width < MIN_WIDTH or height < MIN_HEIGHT:
            too_small.display = True
            for widget_id in ("#playground-banner", "#empty-state", "#main-area", "#status-bar"):
                with suppress(Exception):
                    self.query_one(widget_id).display = False
            too_small.update(
                f"[bold]Terminal too small[/bold]\n"
                f"Need {MIN_WIDTH}x{MIN_HEIGHT}, got {width}x{height}\n"
                "Resize terminal or press q to quit"
            )
            return
        else:
            too_small.display = False
            try:
                self.query_one("#playground-banner", Static).display = True
                self.query_one("#main-area", Horizontal).display = True
                self.query_one("#status-bar", Static).display = True
                self.query_one("#empty-state", Static).display = not bool(self._mount_points)
            except Exception:
                pass

        # Auto-collapse mount panel at narrow widths
        if width < MOUNT_PANEL_COLLAPSE_WIDTH:
            self._mount_panel_auto_collapsed = True
            self.show_mount_panel = False
        else:
            self._mount_panel_auto_collapsed = False
            self.show_mount_panel = True

    async def on_key(self, event: Any) -> None:
        """Handle global key events (quit, escape)."""
        focused = self.focused
        if isinstance(focused, MountPanel):
            if event.key == "up":
                focused.action_move_up()
                event.prevent_default()
                return
            if event.key == "down":
                focused.action_move_down()
                event.prevent_default()
                return
            if event.key in {"enter", "return", "ctrl+m"}:
                mount_point = focused.selected_mount
                if mount_point:
                    browser = self.query_one("#file-browser", FileBrowser)
                    await browser.load_directory(mount_point)
                    self._current_path = mount_point
                    self._update_status_bar()
                event.prevent_default()
                return
            if event.key in {"right", "tab"}:
                self._refocus_table()
                event.prevent_default()
                return
        if event.key == "shift+tab" and not isinstance(focused, (MountPanel, Input)):
            if self.show_mount_panel:
                # Reverse focus: file browser → mount panel
                self.action_focus_mount_panel()
            # Always suppress default Textual focus cycling for shift+tab
            event.prevent_default()
            return
        if event.key in {"enter", "return", "ctrl+m"} and await self._mount_selected_picker_uri():
            event.prevent_default()
            return
        if self.command_visible:
            if event.key in {"enter", "return", "ctrl+m"}:
                await self.action_submit_command()
                event.prevent_default()
                return
            if event.key == "backspace":
                self.action_command_backspace()
                event.prevent_default()
                return
            if event.key == "escape":
                self.action_cancel_command()
                event.prevent_default()
                return
            if getattr(event, "is_printable", False) and getattr(event, "character", None):
                if event.character == ":" and not self.command_buffer:
                    event.prevent_default()
                    return
                self.command_buffer += event.character
                event.prevent_default()
                return
            if event.key == "space":
                self.command_buffer += " "
                event.prevent_default()
                return
            if len(event.key) == 1:
                self.command_buffer += event.key
                event.prevent_default()
                return

        if event.key in {"question_mark", "?"} and event.character == "?":
            focused = self.focused
            if not isinstance(focused, Input):
                self.push_screen(HelpOverlay())
                event.prevent_default()
                return
        if event.key == ":":
            focused = self.focused
            if not isinstance(focused, Input):
                self.command_visible = True
                self.command_buffer = ""
                event.prevent_default()
                return
        if event.key == "q":
            # Don't quit if user is typing in an input
            focused = self.focused
            if isinstance(focused, Input):
                return
            self.exit()
            return
        if event.key == "escape":
            if self.picker_visible and self.picker_input_visible:
                self.picker_input_visible = False
                self._picker_pending_uri = None
                picker_input = self.query_one("#picker-input", Input)
                picker_input.value = ""
                self._update_picker_help_for_row(
                    self.query_one("#connector-picker", DataTable).cursor_row
                )
                self._refocus_table()
                event.prevent_default()
                return

            # Close CRUD input
            if self._crud_mode:
                self._crud_mode = ""
                crud = self.query_one("#crud-input", Input)
                crud.display = False
                crud.value = ""
                self._refocus_table()
                event.prevent_default()
                return

            if self.search_visible:
                self.search_visible = False
                self._refocus_table()
                event.prevent_default()
                return

            if self.command_visible:
                self.command_visible = False
                self.command_buffer = ""
                self._refocus_table()
                event.prevent_default()
                return

            try:
                preview = self.query_one("#file-preview", FilePreview)
                if preview.display:
                    preview.display = False
                    preview.clear_preview()
                    self._refocus_table()
                    event.prevent_default()
            except Exception:
                pass
