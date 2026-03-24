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
    #search-container {
        dock: bottom;
        height: 3;
        display: none;
        padding: 0 1;
    }
    #search-input {
        width: 100%;
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
    ]

    show_mount_panel: reactive[bool] = reactive(True)
    search_visible: reactive[bool] = reactive(False)

    def __init__(self, uris: tuple[str, ...] = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._uris = uris
        self._fs: Any = None
        self._mount_points: list[str] = []
        self._current_path: str = "/"

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
        """Mount backends from URIs or auto-discover from state dir.

        For local:// URIs, uses direct filesystem passthrough so users
        can browse real files on disk without seeding through the API.
        Cloud URIs (s3://, gcs://) go through the full NexusFS mount.
        """
        if self._uris:
            # Check if ALL URIs are local:// — use direct passthrough
            all_local = all(u.startswith("local://") for u in self._uris)
            if all_local:
                return self._resolve_local_direct(self._uris)

            # Mixed or cloud URIs — use full NexusFS mount
            try:
                from nexus.fs import mount

                return await mount(*self._uris)
            except Exception as exc:
                empty = self.query_one("#empty-state", Static)
                empty.update(f"[red]Mount failed:[/red] {exc}")
                return None

        # Auto-discover from state dir (fallback when no URIs given)
        state_dir = os.environ.get("NEXUS_FS_STATE_DIR") or os.path.join(
            __import__("tempfile").gettempdir(), "nexus-fs"
        )
        db_path = os.path.join(state_dir, "metadata.db")
        if not os.path.exists(db_path):
            empty = self.query_one("#empty-state", Static)
            empty.update(
                "[bold]No mounts found[/bold]\n\n"
                "Pass URIs: nexus-fs playground s3://bucket local://./data\n"
                'Or mount first: fs = await nexus.fs.mount("s3://bucket")'
            )
            return None

        # Reconstruct from existing state
        try:
            from nexus.core.router import PathRouter
            from nexus.fs._facade import SlimNexusFS
            from nexus.fs._sqlite_meta import SQLiteMetastore

            metastore = SQLiteMetastore(db_path)
            router = PathRouter(metastore)

            # Read existing mount entries from metastore
            from nexus.contracts.constants import ROOT_ZONE_ID
            from nexus.contracts.types import OperationContext
            from nexus.core.config import BrickServices, KernelServices, PermissionConfig
            from nexus.core.nexus_fs import NexusFS

            ctx = OperationContext(user_id="local", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True)
            kernel = NexusFS(
                metadata_store=metastore,
                permissions=PermissionConfig(enforce=False),
                kernel_services=KernelServices(router=router),
                brick_services=BrickServices(),
                init_cred=ctx,
            )
            return SlimNexusFS(kernel)
        except Exception as exc:
            empty = self.query_one("#empty-state", Static)
            empty.update(f"[red]Auto-discover failed:[/red] {exc}")
            return None

    def _resolve_local_direct(self, uris: tuple[str, ...]) -> Any:
        """Create a LocalDirectFS for local:// URIs (direct filesystem passthrough).

        This lets users browse real files on disk without writing through the API.
        For a single URI, returns a LocalDirectFS. For multiple, returns a
        MultiLocalFS that combines them.
        """
        from pathlib import Path as _Path

        from nexus.fs._tui.local_fs import LocalDirectFS

        if len(uris) == 1:
            raw = uris[0].removeprefix("local://")
            root = _Path(raw).expanduser().resolve()
            if not root.exists():
                root.mkdir(parents=True, exist_ok=True)
            name = root.name or "local"
            return LocalDirectFS(root, f"/local/{name}")

        # Multiple local mounts — use the first one
        # (multi-local support can be added later)
        return self._resolve_local_direct((uris[0],))

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
        """Handle search submission.

        Searches across all mounts concurrently. Shows partial results
        with green/red indicators for which backends responded.
        Highlights matching text in filenames.
        """
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
            name = entry.get("path", "").rstrip("/").rsplit("/", 1)[-1]
            is_dir = entry.get("is_directory", False)

            # Highlight matching text
            name = _highlight_match(name, query_lower)

            if is_dir:
                name = f"[bold cyan]{name}/[/bold cyan]"
            size = "—" if is_dir else _format_size(entry.get("size", 0))
            modified = _format_modified(entry.get("modified_at"))
            table.add_row(name, size, modified)

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

    # -- Actions --

    def action_request_quit(self) -> None:
        """Quit immediately without confirmation dialog."""
        self.exit()

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
        """Copy current file path to clipboard."""
        try:
            browser = self.query_one("#file-browser", FileBrowser)
            path = browser.copy_current_path()
            if path:
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
            self.exit()
            return
        if event.key == "escape":
            if self.search_visible:
                self.search_visible = False
                event.prevent_default()
                return

            try:
                preview = self.query_one("#file-preview", FilePreview)
                if preview.display:
                    preview.display = False
                    preview.clear_preview()
                    event.prevent_default()
            except Exception:
                pass
