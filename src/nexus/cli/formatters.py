"""CLI output formatters - Rich formatting utilities for Nexus CLI."""

from datetime import datetime


def format_timestamp(dt: datetime | str | None) -> str:
    """Format a datetime or the native SDK's ISO timestamp for display."""
    if dt is None:
        return "N/A"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.strftime("%Y-%m-%d %H:%M:%S")
