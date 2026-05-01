"""Compatibility imports for CLI-backed connector base classes.

Canonical implementations live in :mod:`nexus.backends.base.cli_backend`.
"""

from nexus.backends.base.cli_backend import PathCLIBackend, ScopedAuthRequiredError

__all__ = ["PathCLIBackend", "ScopedAuthRequiredError"]
