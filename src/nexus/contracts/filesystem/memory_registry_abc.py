"""Memory registry sub-ABC for filesystem implementations.

Extracted from core/filesystem.py (Issue #2424) following the
``collections.abc`` composition pattern.

Contains: register_memory, unregister_memory, list_memories, get_memory_info
"""

import builtins
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Any


class MemoryRegistryABC(ABC):
    """Memory registration and lookup operations."""

    @abstractmethod
    def register_memory(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        ttl: timedelta | None = None,
    ) -> dict[str, Any]:
        """Register a memory path.

        Args:
            path: Path to register as memory
            name: Optional memory name
            description: Optional description
            created_by: User/agent who created the memory
            tags: Optional tags
            metadata: Optional metadata
            session_id: If provided, memory is session-scoped
            ttl: Time-to-live for auto-expiry

        Returns:
            Memory registration info
        """
        ...

    @abstractmethod
    def unregister_memory(self, path: str) -> bool:
        """Unregister a memory path.

        Args:
            path: Memory path to unregister

        Returns:
            True if unregistered, False if not found
        """
        ...

    @abstractmethod
    def list_memories(self) -> builtins.list[dict]:
        """List all registered memories.

        Returns:
            List of memory info dicts
        """
        ...

    @abstractmethod
    def get_memory_info(self, path: str) -> dict | None:
        """Get memory information.

        Args:
            path: Memory path

        Returns:
            Memory info dict or None if not found
        """
        ...
