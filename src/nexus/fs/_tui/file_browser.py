"""File browser widget — directory listing with name, size, modified columns."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import DataTable, Static

from nexus.fs._tui.auth_guidance import format_runtime_error

MAX_DISPLAY_ENTRIES = 500


def _format_size(size: int) -> str:
    """Format bytes to human-readable size."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.1f} GB"


def _format_modified(iso_str: str | None) -> str:
    """Format ISO timestamp for display."""
    if not iso_str:
        return "—"
    # Show just date + time without seconds
    return iso_str[:16].replace("T", " ")


class FileBrowser(Widget):
    """Right panel: directory listing with columns for name, size, modified.

    Capped at MAX_DISPLAY_ENTRIES with an overflow indicator.

    Attributes:
        current_path: The directory currently being viewed.
        is_loading: Whether a directory load is in progress.
    """

    DEFAULT_CSS = """
    FileBrowser {
        width: 1fr;
    }
    FileBrowser DataTable {
        height: 1fr;
    }
    FileBrowser .empty-message {
        text-align: center;
        padding: 4 2;
        color: $text-muted;
        display: none;
    }
    FileBrowser .error-message {
        text-align: center;
        padding: 4 2;
        color: $error;
    }
    FileBrowser .overflow-indicator {
        dock: bottom;
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-style: italic;
    }
    """

    current_path: reactive[str] = reactive("/")
    is_loading: reactive[bool] = reactive(False)

    class FileSelected(Message):
        """Posted when a file is selected for preview."""

        def __init__(self, path: str, is_directory: bool) -> None:
            self.path = path
            self.is_directory = is_directory
            super().__init__()

    class DirectoryChanged(Message):
        """Posted when navigating into a directory."""

        def __init__(self, path: str) -> None:
            self.path = path
            super().__init__()

    def __init__(self, fs: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fs = fs
        self._entries: list[dict[str, Any]] = []
        self._total_count: int = 0
        self._history: list[str] = []
        self._error: str | None = None

    def compose(self) -> ComposeResult:
        table = DataTable(id="file-table")
        table.cursor_type = "row"
        table.add_columns("Name", "Size", "Modified")
        yield table
        yield Static(
            "Empty folder\n\nPress [bold]b[/bold] to go back",
            id="empty-state",
            classes="empty-message",
        )
        yield Static("", id="overflow", classes="overflow-indicator")

    async def load_directory(self, path: str) -> None:
        """Load directory contents from the filesystem."""
        self.is_loading = True
        self.loading = True  # Textual built-in: shows LoadingIndicator overlay
        self._error = None

        table = self.query_one("#file-table", DataTable)
        table.clear()

        try:
            entries = await self._fs.ls(path, detail=True)
            self._total_count = len(entries)

            # Sort: directories first, then alphabetical
            entries.sort(key=lambda e: (not e.get("is_directory", False), e.get("path", "")))

            if len(entries) > MAX_DISPLAY_ENTRIES:
                self._entries = entries[:MAX_DISPLAY_ENTRIES]
            else:
                self._entries = entries

            for entry in self._entries:
                name = entry.get("path", "").rstrip("/").rsplit("/", 1)[-1]
                is_dir = entry.get("is_directory", False)
                if is_dir:
                    name = f"[bold cyan]{name}/[/bold cyan]"
                size = "—" if is_dir else _format_size(entry.get("size", 0))
                modified = _format_modified(entry.get("modified_at"))
                table.add_row(name, size, modified)

            # Toggle empty state vs table visibility
            empty_state = self.query_one("#empty-state", Static)
            if not self._entries:
                table.display = False
                empty_state.display = True
            else:
                table.display = True
                empty_state.display = False

            # Update overflow indicator
            overflow = self.query_one("#overflow", Static)
            if self._total_count > MAX_DISPLAY_ENTRIES:
                remaining = self._total_count - MAX_DISPLAY_ENTRIES
                overflow.update(f"… and {remaining:,} more files (use / to search)")
            else:
                overflow.update("")

            self.current_path = path

        except Exception as exc:
            self._error = format_runtime_error(path, exc)
            self._entries = []
            self._total_count = 0
            table.display = True
            self.query_one("#empty-state", Static).display = False
            overflow = self.query_one("#overflow", Static)
            overflow.update(f"[red]Error: {self._error}[/red]")

        self.is_loading = False
        self.loading = False  # Hide Textual's LoadingIndicator overlay

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection (Enter key)."""
        if not self._entries:
            return

        idx = event.cursor_row
        if idx >= len(self._entries):
            return

        entry = self._entries[idx]
        path = entry.get("path", "")
        is_dir = entry.get("is_directory", False)

        if is_dir:
            self._history.append(self.current_path)
            self.post_message(self.DirectoryChanged(path))
        else:
            self.post_message(self.FileSelected(path, is_directory=False))

    def go_back(self) -> bool:
        """Navigate to the previous directory.

        Returns:
            True if navigation happened, False if at root.
        """
        if self._history:
            prev = self._history.pop()
            self.post_message(self.DirectoryChanged(prev))
            return True

        # Try parent directory
        if self.current_path != "/" and "/" in self.current_path.rstrip("/"):
            parent = self.current_path.rstrip("/").rsplit("/", 1)[0] or "/"
            self.post_message(self.DirectoryChanged(parent))
            return True

        return False

    def copy_current_path(self) -> str | None:
        """Get the path of the currently highlighted entry."""
        table = self.query_one("#file-table", DataTable)
        if not self._entries:
            return None

        row_idx = table.cursor_row
        if row_idx >= len(self._entries):
            return None

        result: str | None = self._entries[row_idx].get("path")
        return result

    @property
    def entry_count(self) -> int:
        """Number of entries in current directory."""
        return self._total_count
