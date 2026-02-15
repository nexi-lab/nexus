"""Shared version utility for observability modules."""

from __future__ import annotations


def get_nexus_version() -> str:
    """Get the installed nexus-ai-fs package version.

    Returns:
        Version string, or "unknown" if the package is not installed.
    """
    try:
        from importlib.metadata import version

        return version("nexus-ai-fs")
    except Exception:
        return "unknown"
