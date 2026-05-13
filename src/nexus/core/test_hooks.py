"""Core marker for disabled-by-default end-to-end test hooks."""

from __future__ import annotations

from typing import Any


def register_test_hooks(nx: Any) -> None:
    """Mark the filesystem instance as having test hooks enabled."""
    nx._test_hooks_registered = True
