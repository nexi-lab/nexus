"""nexus-fs playground — interactive TUI file browser.

Two-panel layout: mount list (left) + file browser (right) + status bar (bottom).
Keyboard-only navigation. Responsive: collapses mount panel at < 100 cols.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Footer, Header, Input, Static

from nexus.fs._tui.file_browser import FileBrowser
from nexus.fs._tui.file_preview import FilePreview
from nexus.fs._tui.mount_panel import MountPanel

MIN_WIDTH = 80
MIN_HEIGHT = 24
MOUNT_PANEL_COLLAPSE_WIDTH = 100


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
    #empty-state {
        width: 100%;
        content-align: center middle;
        padding: 4 2;
        color: $text-muted;
    }
    #too-small-message {
        width: 100%;
        content-align: center middle;
        display: none;
    }
    """

    BINDINGS = [
        Binding("q", "request_quit", "Quit", priority=True),
        Binding("b", "go_back", "Back"),
        Binding("/", "toggle_search", "Search", key_display="/"),
        Binding("c", "copy_path", "Copy path"),
        Binding("p", "preview_file", "Preview"),
        Binding("m", "toggle_mount_panel", "Mounts"),
        Binding("n", "new_file", "New file"),
        Binding("N", "new_dir", "New dir", key_display="N"),
        Binding("d", "delete_selected", "Delete"),
        Binding("r", "rename_selected", "Rename"),
    ]

    show_mount_panel: reactive[bool] = reactive(True)
    search_visible: reactive[bool] = reactive(False)
    _crud_mode: str = ""  # "", "new_file", "new_dir", "rename", "delete_confirm"

    def __init__(self, uris: tuple[str, ...] = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._uris = uris
        self._fs: Any = None
        self._mount_points: list[str] = []
        self._current_path: str = "/"
        self._crud_rename_source: str = ""

    async def on_mount(self) -> None:
        """Initialize filesystem and load mounts."""
        fs = await self._resolve_filesystem()

        if fs is None:
            return

        self._fs = fs
        self._mount_points = fs.list_mounts()

        if not self._mount_points:
            empty = self.query_one("#empty-state", Static)
            empty.update(
                "[bold]No mounts configured[/bold]\n\n"
                'Run: fs = await nexus.fs.mount("s3://bucket")\n'
                "Then: nexus-fs playground s3://bucket"
            )
            return

        # Build the UI
        await self._build_browser_ui()

    async def _resolve_filesystem(self) -> Any:
        """Mount backends from URIs using direct adapters.

        Always uses direct adapters (LocalDirectFS, S3DirectFS) so users
        see real files immediately — no NexusFS kernel, no empty metastore.
        """
        if self._uris:
            try:
                return self._build_direct_fs(self._uris)
            except Exception as exc:
                empty = self.query_one("#empty-state", Static)
                empty.update(f"[red]Mount failed:[/red] {exc}")
                return None

        # No URIs — auto-discover from mounts.json in state dir
        import json

        state_dir = os.environ.get("NEXUS_FS_STATE_DIR") or os.path.join(
            __import__("tempfile").gettempdir(), "nexus-fs"
        )
        mounts_file = os.path.join(state_dir, "mounts.json")
        if os.path.exists(mounts_file):
            try:
                with open(mounts_file) as f:
                    saved_uris = json.load(f)
                if saved_uris:
                    return self._build_direct_fs(tuple(saved_uris))
            except Exception:
                pass  # Fall through to empty state

        empty = self.query_one("#empty-state", Static)
        empty.update(
            "[bold]No mounts found[/bold]\n\n"
            "nexus-fs playground s3://bucket\n"
            "nexus-fs playground local://./data\n"
            "nexus-fs playground s3://bucket local://./data"
        )
        return None

    def _build_direct_fs(self, uris: tuple[str, ...]) -> Any:
        """Build direct filesystem adapters from URIs.

        local:// → LocalDirectFS (pathlib)
        s3://    → S3DirectFS (boto3)
        Multiple → MultiDirectFS (combines them)
        """
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
                    f"Unsupported scheme in '{uri}'. "
                    "Playground supports local:// and s3://"
                )

        if len(backends) == 1:
            return backends[0]
        return MultiDirectFS(backends)

    async def _build_browser_ui(self) -> None:
        """Build the file browser UI after mounts are resolved."""
        main = self.query_one("#main-area", Horizontal)

        # Mount panel
        mount_panel = MountPanel(self._fs, self._mount_points, id="mount-panel")
        await main.mount(mount_panel)

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

        # Load first mount and focus the file table
        if self._mount_points:
            await browser.load_directory(self._mount_points[0])
            self._current_path = self._mount_points[0]
            self._update_status_bar(announce=False)
            browser.query_one("#file-table").focus()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="too-small-message")
        empty = Static(
            "[dim]Loading mounts…[/dim]",
            id="empty-state",
        )
        empty.can_focus = True
        yield empty
        yield Horizontal(id="main-area")
        yield Input(
            placeholder="Search files… (Enter to search, Escape to cancel)",
            id="search-input",
        )
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

        status_text = (
            f"{mount_count} mount(s) | {file_count} entries | {self._current_path}"
        )
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

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Route input submissions to search or CRUD handler."""
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
            entries = await self._fs.ls(mount, detail=True, recursive=True)
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
                f"[green]●[/green]{mp.split('/')[-1]}" if mp in succeeded
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
                await self._fs.write(path, b"")
                self.notify(f"Created: {value}", timeout=2)
                await browser.load_directory(self._current_path)
                self._update_status_bar()
            except Exception as exc:
                self.notify(f"Create failed: {exc}", severity="error", timeout=3)

        elif mode == "new_dir":
            path = f"{self._current_path.rstrip('/')}/{value}"
            try:
                await self._fs.mkdir(path)
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
                await self._fs.rename(old_path, new_path)
                self.notify(f"Renamed → {new_name}", timeout=2)
                await browser.load_directory(self._current_path)
                self._update_status_bar()
            except Exception as exc:
                self.notify(f"Rename failed: {exc}", severity="error", timeout=3)

        self._refocus_table()

    def _refocus_table(self) -> None:
        """Return focus to the file table after CRUD operations."""
        import contextlib

        with contextlib.suppress(Exception):
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
                await self._fs.rmdir(path, recursive=True)
                self.notify(f"Deleted directory: {name}", timeout=2)
            else:
                await self._fs.delete(path)
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
        try:
            browser = self.query_one("#file-browser", FileBrowser)
            browser.go_back()
        except Exception:
            pass

    def action_toggle_search(self) -> None:
        """Toggle search input."""
        self.search_visible = not self.search_visible

    def watch_search_visible(self, visible: bool) -> None:
        """Show/hide search input."""
        try:
            search = self.query_one("#search-input", Input)
            search.display = visible
            if visible:
                search.focus()
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

    def watch_show_mount_panel(self, show: bool) -> None:
        """Show/hide mount panel."""
        try:
            panel = self.query_one("#mount-panel", MountPanel)
            panel.display = show
        except Exception:
            pass

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
            too_small.update(
                f"[bold]Terminal too small[/bold]\n"
                f"Need {MIN_WIDTH}x{MIN_HEIGHT}, got {width}x{height}\n"
                "Resize terminal or press q to quit"
            )
            return
        else:
            too_small.display = False

        # Auto-collapse mount panel at narrow widths
        if width < MOUNT_PANEL_COLLAPSE_WIDTH:
            self.show_mount_panel = False
        else:
            self.show_mount_panel = True

    async def on_key(self, event: Any) -> None:
        """Handle global key events (quit, escape)."""
        if event.key == "q":
            # Don't quit if user is typing in an input
            focused = self.focused
            if isinstance(focused, Input):
                return
            self.exit()
            return
        if event.key == "escape":
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

            try:
                preview = self.query_one("#file-preview", FilePreview)
                if preview.display:
                    preview.display = False
                    preview.clear_preview()
                    self._refocus_table()
                    event.prevent_default()
            except Exception:
                pass
