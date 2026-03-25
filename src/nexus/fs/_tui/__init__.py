"""nexus-fs playground — interactive TUI file browser.

Two-panel layout: mount list (left) + file browser (right) + status bar (bottom).
Keyboard-only navigation. Responsive: collapses mount panel at < 100 cols.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, cast

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Input, Static

from nexus.fs._tui.file_browser import FileBrowser
from nexus.fs._tui.file_preview import FilePreview
from nexus.fs._tui.mount_panel import MountPanel

MIN_WIDTH = 80
MIN_HEIGHT = 24
MOUNT_PANEL_COLLAPSE_WIDTH = 100


class ContextualNexusFS:
    """Kernel wrapper that uses a caller-selected operation context."""

    def __init__(self, kernel: Any, *, user_id: str = "local") -> None:
        from nexus.contracts.constants import ROOT_ZONE_ID
        from nexus.contracts.types import OperationContext

        self._kernel = kernel
        self._ctx = OperationContext(
            user_id=user_id,
            groups=[],
            zone_id=ROOT_ZONE_ID,
            is_admin=True,
        )

    async def read(self, path: str) -> bytes:
        return cast(bytes, await self._kernel.sys_read(path, context=self._ctx))

    async def read_range(self, path: str, start: int, end: int) -> bytes:
        return cast(bytes, await self._kernel.read_range(path, start, end, context=self._ctx))

    async def write(self, path: str, content: bytes) -> dict[str, Any]:
        return cast(dict[str, Any], await self._kernel.write(path, content, context=self._ctx))

    async def ls(
        self,
        path: str = "/",
        detail: bool = False,
        recursive: bool = False,
    ) -> list[str] | list[dict[str, Any]]:
        return cast(
            list[str] | list[dict[str, Any]],
            await self._kernel.sys_readdir(
                path,
                recursive=recursive,
                details=detail,
                context=self._ctx,
            ),
        )

    async def stat(self, path: str) -> dict[str, Any] | None:
        from nexus.fs._facade import SlimNexusFS

        return await SlimNexusFS(self._kernel).stat(path)

    async def mkdir(self, path: str, parents: bool = True) -> None:
        await self._kernel.mkdir(path, parents=parents, exist_ok=True, context=self._ctx)

    async def rmdir(self, path: str, recursive: bool = False) -> None:
        await self._kernel.sys_rmdir(path, recursive=recursive, context=self._ctx)

    async def delete(self, path: str) -> None:
        await self._kernel.sys_unlink(path, context=self._ctx)

    async def rename(self, old_path: str, new_path: str) -> None:
        await self._kernel.sys_rename(old_path, new_path, context=self._ctx)

    async def exists(self, path: str) -> bool:
        return cast(bool, await self._kernel.sys_access(path, context=self._ctx))

    async def copy(self, src: str, dst: str) -> dict[str, Any]:
        content = await self.read(src)
        return await self.write(dst, content)

    def list_mounts(self) -> list[str]:
        mounts = []
        for item in self._kernel.router.list_mounts():
            mount_point = getattr(item, "mount_point", item)
            mounts.append(str(mount_point))
        filtered = [mount for mount in mounts if mount != "/"]
        return filtered or mounts

    async def close(self) -> None:
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
        Binding(":", "toggle_command", "Command", key_display=":", priority=True),
        Binding("enter", "submit_command", "", show=False, priority=True),
        Binding("ctrl+m", "submit_command", "", show=False, priority=True),
        Binding("backspace", "command_backspace", "", show=False, priority=True),
        Binding("a", "show_connector_picker", "Add Mount"),
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
    command_visible: reactive[bool] = reactive(False)
    command_buffer: reactive[str] = reactive("")
    picker_visible: reactive[bool] = reactive(False)
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
                    return await self._build_filesystem(tuple(saved_uris))
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

    async def _build_filesystem(self, uris: tuple[str, ...]) -> Any:
        """Build a hybrid filesystem for playground mounts."""
        from nexus.fs import mount as mount_fs
        from nexus.fs._tui.direct_fs import MultiDirectFS

        direct_uris = tuple(uri for uri in uris if uri.startswith(("local://", "s3://")))
        generic_uris = tuple(uri for uri in uris if uri not in direct_uris)
        backends: list[Any] = []

        if direct_uris:
            backends.append(self._build_direct_fs(direct_uris))

        for uri in generic_uris:
            facade = await mount_fs(uri)
            backends.append(
                ContextualNexusFS(
                    facade.kernel,
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
            "[dim]Browse connectors below, press Enter to mount, or press a later to reopen this picker.[/dim]"
        )

        picker = DataTable(id="connector-picker")
        picker.cursor_type = "row"
        picker.add_columns("Connector", "Mode")
        for uri, mode in self._supported_connector_rows():
            picker.add_row(uri, mode)
        await main.mount(picker)
        self.picker_visible = True
        if picker.row_count:
            picker.move_cursor(row=0, column=0)
        picker.focus()
        self.query_one("#status-bar", Static).update(
            "[dim]Connector picker | arrows to browse | Enter to mount | : for auth/help[/dim]"
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
        await self._mount_uri(uri)
        return True

    def compose(self) -> ComposeResult:
        yield Header()
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
        await self._mount_uri(uri)

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
                self.notify(
                    "Usage: /mount local://./data or /mount s3://bucket[/prefix]",
                    severity="warning",
                    timeout=4,
                )
                self._refocus_table()
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
        next_uris = (*self._uris, uri)
        try:
            fs = await self._build_filesystem(next_uris)
        except Exception as exc:
            self.notify(self._mount_error_message(uri, exc), severity="error", timeout=6)
            return

        self._uris = next_uris
        self._fs = fs
        self._mount_points = fs.list_mounts()
        self._persist_mounts()
        await self._reset_browser_ui()
        self.notify(f"Mounted {uri}", timeout=3)

    async def _touch_path(self, name: str) -> None:
        """Create an empty file in the current directory."""
        if not self._fs:
            return
        path = self._resolve_command_path(name)
        try:
            await self._fs.write(path, b"")
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
            await self._fs.delete(path)
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
            from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService
            from nexus.bricks.auth.unified_service import UnifiedAuthService
            from nexus.cli.commands.oauth import get_token_manager

            oauth_service = OAuthCredentialService(token_manager=get_token_manager())
            auth_service = UnifiedAuthService(oauth_service=oauth_service)
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
        """Persist current mount URIs for the next playground launch."""
        state_dir = os.environ.get("NEXUS_FS_STATE_DIR") or os.path.join(
            __import__("tempfile").gettempdir(), "nexus-fs"
        )
        os.makedirs(state_dir, exist_ok=True)
        mounts_file = os.path.join(state_dir, "mounts.json")
        with open(mounts_file, "w") as f:
            json.dump(list(self._uris), f)

    def _mount_error_message(self, uri: str, exc: Exception) -> str:
        """Render actionable mount failures."""
        if uri.startswith("s3://"):
            return f"{exc}. Run `nexus-fs auth connect s3 native` or configure AWS credentials, then retry /mount."
        if uri.startswith("gws://"):
            return (
                f"{exc}. Run /auth gws for step-by-step auth guidance. "
                "If you use multiple Google accounts, set NEXUS_FS_USER_EMAIL before mounting."
            )
        return str(exc)

    def _auth_guidance(self, service: str) -> str:
        """Return concise auth guidance for playground-supported backends."""
        if service == "s3":
            return (
                "For S3: 1. run `nexus-fs auth connect s3 native` or set AWS credentials; "
                "2. run `/auth test s3`; 3. run `/mount s3://bucket`."
            )
        if service == "gws":
            return (
                "For Google Workspace: 1. set NEXUS_OAUTH_GOOGLE_CLIENT_ID and "
                "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET; 2. run `nexus-fs auth connect gws oauth "
                "--user-email you@example.com`; 3. run `/auth test gws`; "
                "4. if needed set `NEXUS_FS_USER_EMAIL=you@example.com`; "
                "5. run `/mount gws://drive` or `/mount gws://gmail`."
            )
        if service == "gcs":
            return (
                "For GCS: 1. run `nexus-fs auth connect gcs native` or "
                "`gcloud auth application-default login`; 2. run `/auth test gcs`."
            )
        if service == "local":
            return "Local mounts do not require auth. Use `/mount local://./data`."
        return f"No dedicated playground auth guide for {service}. Use `/connectors` to list supported targets."

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

        rows: list[tuple[str, str]] = [
            ("local://./data", "mountable"),
            ("s3://bucket/<prefix>", "mountable auth:s3"),
            ("gcs://project/bucket", "mountable auth:gcs"),
        ]
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
        if connector_name == "gcalendar_connector":
            return "calendar://primary"
        if connector_name == "slack_connector":
            return "slack://workspace"
        if connector_name == "x_connector":
            return "x://timeline"
        if connector_name == "hn_connector":
            return "hn://top"
        if connector_name.startswith("gws_"):
            return f"gws://{connector_name.removeprefix('gws_')}"
        return None

    def _connector_auth_service(self, connector_name: str, service_name: str | None) -> str | None:
        """Map connector targets to the auth flow users should follow."""
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
            ("gws://", "gdrive://", "gmail://", "calendar://", "slack://", "x://")
        ):
            return "local"

        try:
            from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService
            from nexus.cli.commands.oauth import get_token_manager
        except Exception:
            return "local"

        try:
            oauth_service = OAuthCredentialService(token_manager=get_token_manager())
            creds = await oauth_service.list_credentials()
        except Exception:
            return "local"

        if uri.startswith(("gws://", "gdrive://", "gmail://", "calendar://")):
            providers = {"google"}
        elif uri.startswith("slack://"):
            providers = {"slack"}
        else:
            providers = {"x", "twitter"}

        emails = sorted(
            {
                str(cred.get("user_email"))
                for cred in creds
                if cred.get("provider") in providers and cred.get("user_email")
            }
        )
        return emails[0] if len(emails) == 1 else "local"

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

    async def action_show_connector_picker(self) -> None:
        """Open the interactive connector picker."""
        await self._show_connector_picker("Mount another connector")

    async def action_submit_command(self) -> None:
        """Submit the current command buffer."""
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
