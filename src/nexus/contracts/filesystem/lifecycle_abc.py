"""Lifecycle sub-ABC for filesystem implementations.

Extracted from core/filesystem.py (Issue #2424) following the
``collections.abc`` composition pattern (Sized + Iterable → Collection).

Contains: agent_id, zone_id, close, __enter__, __exit__
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LifecycleABC(ABC):
    """Lifecycle management for filesystem instances.

    Provides identity properties (agent_id, zone_id), resource cleanup (close),
    and context-manager support.
    """

    @property
    @abstractmethod
    def agent_id(self) -> str | None:
        """Agent ID for this filesystem instance."""
        ...

    @property
    @abstractmethod
    def zone_id(self) -> str | None:
        """Zone ID for this filesystem instance."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Close the filesystem and release resources."""
        ...

    def __enter__(self) -> LifecycleABC:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()
