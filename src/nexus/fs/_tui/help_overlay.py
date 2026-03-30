"""Help overlay — full-screen keybinding reference for the playground TUI.

Activated with ``?`` key. Shows all keybindings grouped by intent.
Press any key to dismiss.

See Issue #3508.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Static

# ---------------------------------------------------------------------------
# Keybinding definitions (canonical source for the help overlay)
# ---------------------------------------------------------------------------

GLOBAL_BINDINGS: list[tuple[str, str]] = [
    ("?", "Help overlay"),
    ("q", "Quit"),
    ("b", "Back"),
]

NAVIGATION_BINDINGS: list[tuple[str, str]] = [
    ("Tab", "Switch panel"),
    ("\u2191/\u2193", "Move up/down"),
    ("Enter", "Select / open"),
    ("Esc", "Cancel / close"),
    ("m", "Focus mount panel"),
]

FILE_OPERATION_BINDINGS: list[tuple[str, str]] = [
    ("n", "New file"),
    ("N", "New directory"),
    ("d", "Delete"),
    ("r", "Rename"),
    ("p", "Preview file"),
    ("c", "Copy path"),
]

MOUNT_AND_SEARCH_BINDINGS: list[tuple[str, str]] = [
    ("a", "Add mount"),
    ("u", "Unmount"),
    ("/", "Search"),
    (":", "Command mode"),
]

ALL_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Global", GLOBAL_BINDINGS),
    ("Navigation", NAVIGATION_BINDINGS),
    ("File Operations", FILE_OPERATION_BINDINGS),
    ("Mount & Search", MOUNT_AND_SEARCH_BINDINGS),
]

# Flat set of keys in the help overlay (used by drift tests).
ALL_HELP_KEYS: set[str] = {key for _, bindings in ALL_GROUPS for key, _ in bindings}


def _render_help_text() -> str:
    """Build Rich-markup text for all binding groups."""
    lines: list[str] = ["[bold]Keybinding Reference[/bold]", ""]
    for group_name, bindings in ALL_GROUPS:
        lines.append(f"[bold cyan]\u2500\u2500\u2500 {group_name} \u2500\u2500\u2500[/bold cyan]")
        for key, action in bindings:
            lines.append(f"  [bold cyan]{key:<12}[/bold cyan] {action}")
        lines.append("")
    lines.append("[dim]Press any key to dismiss[/dim]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Modal screen
# ---------------------------------------------------------------------------


class HelpOverlay(ModalScreen[None]):
    """Full-screen keybinding reference overlay, dismissed on any key."""

    DEFAULT_CSS = """
    HelpOverlay {
        align: center middle;
    }
    #help-container {
        width: 70%;
        max-width: 80;
        height: 80%;
        background: $surface;
        border: heavy $primary;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-container"):
            yield Static(_render_help_text(), id="help-text")

    def on_key(self, event: Key) -> None:
        """Dismiss on any keypress."""
        event.stop()
        self.dismiss()
