"""Centralized theme for Nexus CLI.

All CLI color markup should use these semantic tokens instead of bare color names.
Terminal colors use ANSI-16 approximations (e.g. amber -> yellow).

NO_COLOR / FORCE_COLOR: Rich handles both automatically. When NO_COLOR is set,
colors are stripped but text decorations (bold/dim/italic) and status icons
(which are chosen to be meaningful without color) are preserved.

Token usage guide:
    nexus.success  — confirmations, healthy status, valid states
    nexus.warning  — deprecations, caution, starting/stopping states
    nexus.error    — failures, disconnected, invalid states
    nexus.info     — informational messages, general highlighted data
    nexus.accent   — brand accents, tips, table headers
    nexus.muted    — secondary text, timestamps, deemphasized content
    nexus.hint     — fix suggestions, help text, instructions
    nexus.path     — file paths, URIs, URLs
    nexus.value    — data values, counts, entity IDs, general highlighted data
    nexus.label    — section headers, table titles, key names
    nexus.identity — agent identities, user IDs
    nexus.reference — URNs, zone IDs, external references

Table column conventions:
    Names / entity IDs  -> style="nexus.value"
    Timestamps / dates  -> style="nexus.muted"
    File paths          -> style="nexus.path"
    Status columns      -> inline per-value tokens (nexus.success/warning/error)
    Header style        -> header_style="bold nexus.accent"

Usage::

    from nexus.cli.theme import console, print_error, STATUS_OK
    console.print(f"{STATUS_OK} Connected")
    console.print("[nexus.value]42[/nexus.value] records found")
    print_error("Connection failed", "host unreachable")
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.theme import Theme

NEXUS_THEME = Theme(
    {
        # Status — use for state indicators and feedback messages
        "nexus.success": "green",
        "nexus.warning": "yellow",
        "nexus.error": "red",
        "nexus.info": "cyan",
        # Brand
        "nexus.accent": "yellow",
        # Text hierarchy
        "nexus.muted": "dim",
        "nexus.hint": "dim italic",
        # Data types
        "nexus.path": "dim cyan",
        "nexus.value": "cyan",
        "nexus.label": "bold",
        "nexus.identity": "magenta",
        "nexus.reference": "blue",
    }
)

console = Console(theme=NEXUS_THEME)
err_console = Console(theme=NEXUS_THEME, stderr=True)

# Status icon constants — always pair symbol + semantic color.
STATUS_OK = "[nexus.success]\u2713[/nexus.success]"
STATUS_WARN = "[nexus.warning]\u26a0[/nexus.warning]"
STATUS_ERROR = "[nexus.error]\u2717[/nexus.error]"
STATUS_UNSET = "[nexus.muted]\u25cb[/nexus.muted]"


# ---------------------------------------------------------------------------
# Convenience helpers — DRY wrappers for the most common print patterns.
# ---------------------------------------------------------------------------


def print_error(label: str, message: Any = "") -> None:
    """Print ``[nexus.error]<label>:[/nexus.error] <message>``."""
    if message:
        console.print(f"[nexus.error]{label}:[/nexus.error] {message}")
    else:
        console.print(f"[nexus.error]{label}[/nexus.error]")


def print_warning(label: str, message: Any = "") -> None:
    """Print ``[nexus.warning]<label>:[/nexus.warning] <message>``."""
    if message:
        console.print(f"[nexus.warning]{label}:[/nexus.warning] {message}")
    else:
        console.print(f"[nexus.warning]{label}[/nexus.warning]")


def print_success(label: str, message: Any = "") -> None:
    """Print ``[nexus.success]<label>:[/nexus.success] <message>``."""
    if message:
        console.print(f"[nexus.success]{label}:[/nexus.success] {message}")
    else:
        console.print(f"[nexus.success]{label}[/nexus.success]")


def print_info(label: str, message: Any = "") -> None:
    """Print ``[nexus.info]<label>:[/nexus.info] <message>``."""
    if message:
        console.print(f"[nexus.info]{label}:[/nexus.info] {message}")
    else:
        console.print(f"[nexus.info]{label}[/nexus.info]")
