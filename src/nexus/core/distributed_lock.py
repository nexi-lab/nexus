"""Distributed lock manager interface (ABCs and Protocols).

This module provides the lock manager abstractions for coordinating
access to resources across multiple Nexus nodes.

Architecture:
- LockManagerProtocol: Abstract interface for lock manager
- LockManagerBase: Abstract base class with common functionality

Concrete implementations live outside core/:
- ``nexus.raft.lock_manager.RaftLockManager``: Raft consensus locks

References:
    - docs/architecture/federation-memo.md §6.9
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# =============================================================================
# Low-level Store Protocol
# =============================================================================

@runtime_checkable
class LockStoreProtocol(Protocol):
    """Protocol for lock-capable metadata stores (e.g., RaftMetadataStore).

    Captures the interface that RaftLockManager needs, decoupling
    the kernel lock manager from the concrete storage driver
    (KERNEL-ARCHITECTURE.md §1).
    """

    def acquire_lock(
        self,
        lock_key: str,
        holder_id: str,
        *,
        max_holders: int = 1,
        ttl_secs: int = 30,
    ) -> bool:
        """Atomically acquire a lock."""
        ...

    def release_lock(self, lock_key: str, holder_id: str) -> bool:
        """Release a lock held by holder_id."""
        ...

    def extend_lock(self, lock_key: str, holder_id: str, ttl_secs: int) -> bool:
        """Extend a lock's TTL."""
        ...

    def get_lock_info(self, lock_key: str) -> dict[str, Any] | None:
        """Get information about a lock."""
        ...

    def list_locks(self, *, prefix: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """List active locks matching prefix."""
        ...

    def force_release_lock(self, lock_key: str) -> bool:
        """Force-release all holders of a lock."""
        ...

    def get(self, key: str) -> Any:
        """Get a value by key (used for health checks)."""
        ...

# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class LockInfo:
    """Information about a lock on a resource.

    Returned by get_lock_info() and list_locks().
    """

    path: str
    mode: Literal["mutex", "semaphore"]
    max_holders: int
    holders: "list[HolderInfo]"
    fence_token: int

@dataclass
class HolderInfo:
    """Information about a single lock holder."""

    lock_id: str
    holder_info: str
    acquired_at: float  # Unix timestamp
    expires_at: float  # Unix timestamp

@dataclass
class ExtendResult:
    """Result of a lock extend (heartbeat) operation."""

    success: bool
    lock_info: LockInfo | None = None

# =============================================================================
# Abstract Interface (Protocol)
# =============================================================================

@runtime_checkable
class LockManagerProtocol(Protocol):
    """Protocol defining the lock manager interface.

    This protocol allows different backend implementations:
    - Raft consensus locks (default, implemented as RaftLockManager)

    All implementations must provide these async methods.
    """

    async def acquire(
        self,
        zone_id: str,
        path: str,
        timeout: float = 30.0,
        ttl: float = 30.0,
        max_holders: int = 1,
    ) -> str | None:
        """Acquire a distributed lock or semaphore slot.

        Args:
            zone_id: Zone ID for the lock
            path: Path to lock
            timeout: Maximum time to wait for lock
            ttl: Lock TTL (auto-expires after this)
            max_holders: Maximum concurrent holders (1=mutex, >1=semaphore)

        Returns:
            Lock ID if acquired, None on timeout
        """
        ...

    async def release(
        self,
        lock_id: str,
        zone_id: str,
        path: str,
    ) -> bool:
        """Release a distributed lock.

        Args:
            lock_id: Lock ID from acquire()
            zone_id: Zone ID
            path: Path that was locked

        Returns:
            True if released, False if not owned or expired
        """
        ...

    async def extend(
        self,
        lock_id: str,
        zone_id: str,
        path: str,
        ttl: float = 30.0,
    ) -> ExtendResult:
        """Extend lock TTL (heartbeat).

        Args:
            lock_id: Lock ID from acquire()
            zone_id: Zone ID
            path: Path that was locked
            ttl: New TTL in seconds

        Returns:
            ExtendResult with success flag and updated lock info
        """
        ...

    async def get_lock_info(
        self,
        zone_id: str,
        path: str,
    ) -> LockInfo | None:
        """Get information about a lock.

        Args:
            zone_id: Zone ID
            path: Resource path

        Returns:
            LockInfo if locked, None if not locked
        """
        ...

    async def is_locked(self, zone_id: str, path: str) -> bool:
        """Check if a path is currently locked."""
        ...

    async def list_locks(
        self,
        zone_id: str,
        pattern: str = "",
        limit: int = 100,
    ) -> list[LockInfo]:
        """List active locks for a zone.

        Args:
            zone_id: Zone ID to list locks for
            pattern: Optional path filter
            limit: Maximum number of results

        Returns:
            List of LockInfo for active locks
        """
        ...

    async def force_release(
        self,
        zone_id: str,
        path: str,
    ) -> bool:
        """Force-release all holders of a lock (admin operation).

        Args:
            zone_id: Zone ID
            path: Resource path to force-release

        Returns:
            True if a lock was found and released, False if no lock exists
        """
        ...

    async def health_check(self) -> bool:
        """Check if the lock manager is healthy."""
        ...

class LockManagerBase(ABC):
    """Abstract base class for lock manager implementations.

    Provides common functionality and enforces the interface contract.
    Subclasses must implement all abstract methods.
    """

    DEFAULT_TTL = 30.0  # Default lock TTL in seconds
    DEFAULT_TIMEOUT = 30.0  # Default acquisition timeout

    @abstractmethod
    async def acquire(
        self,
        zone_id: str,
        path: str,
        timeout: float = DEFAULT_TIMEOUT,
        ttl: float = DEFAULT_TTL,
        max_holders: int = 1,
    ) -> str | None:
        """Acquire a distributed lock or semaphore slot.

        Args:
            zone_id: Zone ID for the lock
            path: Path to lock
            timeout: Maximum time to wait
            ttl: Lock TTL (auto-expires)
            max_holders: Maximum concurrent holders (1=mutex, >1=semaphore)

        Returns:
            Lock ID if acquired, None on timeout
        """
        pass

    @abstractmethod
    async def release(
        self,
        lock_id: str,
        zone_id: str,
        path: str,
    ) -> bool:
        """Release a distributed lock."""
        pass

    @abstractmethod
    async def extend(
        self,
        lock_id: str,
        zone_id: str,
        path: str,
        ttl: float = DEFAULT_TTL,
    ) -> ExtendResult:
        """Extend lock TTL (heartbeat)."""
        pass

    @abstractmethod
    async def get_lock_info(
        self,
        zone_id: str,
        path: str,
    ) -> LockInfo | None:
        """Get information about a lock."""
        pass

    async def is_locked(self, zone_id: str, path: str) -> bool:
        """Check if a path is currently locked. Override for efficiency."""
        info = await self.get_lock_info(zone_id, path)
        return info is not None

    @abstractmethod
    async def list_locks(
        self,
        zone_id: str,
        pattern: str = "",
        limit: int = 100,
    ) -> list[LockInfo]:
        """List active locks for a zone."""
        pass

    @abstractmethod
    async def force_release(
        self,
        zone_id: str,
        path: str,
    ) -> bool:
        """Force-release all holders of a lock (admin operation)."""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the lock manager is healthy."""
        pass
