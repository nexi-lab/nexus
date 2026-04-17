"""Thin entry point for `nexus auth`.

Command handlers live in nexus.bricks.auth.cli_commands — this module exists
so the `nexus` CLI's lazy command loader can import the `auth` Click group
from its expected location.
"""

from __future__ import annotations

from nexus.bricks.auth.cli_commands import auth

__all__ = ["auth"]
