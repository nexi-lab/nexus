"""CLI output formatters - Rich formatting utilities for Nexus CLI."""


from datetime import datetime


def format_timestamp(dt: datetime | None) -> str:
    """Format datetime as string.

    Args:
        dt: Datetime object

    Returns:
        Formatted timestamp string
    """
    if dt is None:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M:%S")
