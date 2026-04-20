"""Thin entry point for `nexus daemon` (#3804).

Command handlers live in nexus.bricks.auth.daemon.cli — this module exists
so the `nexus` CLI's lazy command loader can import the `daemon` Click group
from its expected location.
"""

from __future__ import annotations

from nexus.bricks.auth.daemon.cli import daemon

__all__ = ["daemon"]
