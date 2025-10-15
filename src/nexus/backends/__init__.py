"""Storage backends for Nexus."""

from nexus.backends.backend import Backend
from nexus.backends.local import LocalBackend

__all__ = ["Backend", "LocalBackend"]
