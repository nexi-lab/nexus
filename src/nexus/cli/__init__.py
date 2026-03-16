"""Nexus CLI package.

The package keeps its public re-exports lazy so submodules like
``nexus.cli.exit_codes`` do not eagerly import the full CLI stack.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "main",
    "console",
    "get_filesystem",
    "open_filesystem",
    "handle_error",
    "add_backend_options",
]

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "main": ("nexus.cli.main", "main"),
    "console": ("nexus.cli.utils", "console"),
    "get_filesystem": ("nexus.cli.utils", "get_filesystem"),
    "open_filesystem": ("nexus.cli.utils", "open_filesystem"),
    "handle_error": ("nexus.cli.utils", "handle_error"),
    "add_backend_options": ("nexus.cli.utils", "add_backend_options"),
}


def __getattr__(name: str) -> Any:
    """Load CLI exports on demand."""
    if name not in _LAZY_IMPORTS:
        raise AttributeError(f"module 'nexus.cli' has no attribute {name!r}")

    module_name, attr_name = _LAZY_IMPORTS[name]
    module = importlib.import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
