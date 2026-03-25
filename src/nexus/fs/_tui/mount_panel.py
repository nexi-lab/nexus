"""Mount panel widget — left sidebar showing mounted backends with status."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


@dataclass
class MountInfo:
    """Runtime state for a single mount."""

    mount_point: str
    status: str = "checking"  # checking | connected | error
    latency_ms: float | None = None
    error: str | None = None


class MountPanel(Widget):
    """Left sidebar showing mounted backends with live status indicators.

    Attributes:
        mounts: Reactive list of MountInfo objects.
        selected_index: Currently highlighted mount index.
    """

    DEFAULT_CSS = """
    MountPanel {
        width: 30;
        dock: left;
        border-right: solid $surface-lighten-2;
        padding: 0 1;
    }
    MountPanel .mount-title {
        text-style: bold;
        padding: 1 0 0 0;
    }
    MountPanel .mount-entry {
        padding: 0 0 0 1;
    }
    MountPanel .mount-entry.selected {
        background: $accent;
    }
    """

    selected_index: reactive[int] = reactive(0)

    class MountSelected(Message):
        """Posted when a mount is selected."""

        def __init__(self, mount_point: str) -> None:
            self.mount_point = mount_point
            super().__init__()

    def __init__(
        self,
        fs: Any,
        mount_points: list[str],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._fs = fs
        self._mount_infos: list[MountInfo] = [MountInfo(mount_point=mp) for mp in mount_points]

    def compose(self) -> ComposeResult:
        yield Static("Mounts", classes="mount-title")
        for i, info in enumerate(self._mount_infos):
            classes = "mount-entry selected" if i == 0 else "mount-entry"
            yield Static(self._render_mount(info), id=f"mount-{i}", classes=classes)

    def _render_mount(self, info: MountInfo) -> str:
        """Render a single mount entry with status indicator."""
        if info.status == "checking":
            return f"[yellow]…[/yellow] {info.mount_point}"
        elif info.status == "connected":
            latency = f" ({info.latency_ms:.0f}ms)" if info.latency_ms is not None else ""
            return f"[green]●[/green] {info.mount_point}{latency}"
        else:
            return f"[red]●[/red] {info.mount_point}"

    async def on_mount(self) -> None:
        """Start connectivity checks when widget is mounted."""
        for i, info in enumerate(self._mount_infos):
            asyncio.create_task(self._check_connectivity(i, info))

        if self._mount_infos:
            self.post_message(self.MountSelected(self._mount_infos[0].mount_point))

    async def _check_connectivity(self, index: int, info: MountInfo) -> None:
        """Check connectivity to a single mount."""
        start = time.perf_counter()
        try:
            await asyncio.wait_for(self._fs.ls(info.mount_point), timeout=3.0)
            info.latency_ms = (time.perf_counter() - start) * 1000
            info.status = "connected"
        except Exception as exc:
            info.status = "error"
            info.error = str(exc)

        widget = self.query_one(f"#mount-{index}", Static)
        widget.update(self._render_mount(info))

    def watch_selected_index(self, old: int, new: int) -> None:
        """Update visual selection when index changes."""
        if not self._mount_infos:
            return

        new = max(0, min(new, len(self._mount_infos) - 1))
        if new != self.selected_index:
            self.selected_index = new
            return

        try:
            old_widget = self.query_one(f"#mount-{old}", Static)
            old_widget.remove_class("selected")
        except Exception:
            pass

        try:
            new_widget = self.query_one(f"#mount-{new}", Static)
            new_widget.add_class("selected")
        except Exception:
            pass

    def action_move_up(self) -> None:
        """Move selection up."""
        if self.selected_index > 0:
            self.selected_index -= 1
            self.post_message(
                self.MountSelected(self._mount_infos[self.selected_index].mount_point)
            )

    def action_move_down(self) -> None:
        """Move selection down."""
        if self.selected_index < len(self._mount_infos) - 1:
            self.selected_index += 1
            self.post_message(
                self.MountSelected(self._mount_infos[self.selected_index].mount_point)
            )

    @property
    def selected_mount(self) -> str | None:
        """Currently selected mount point."""
        if not self._mount_infos:
            return None
        idx = max(0, min(self.selected_index, len(self._mount_infos) - 1))
        result: str = self._mount_infos[idx].mount_point
        return result

    @property
    def mount_infos(self) -> list[MountInfo]:
        """Access mount info objects."""
        return self._mount_infos
