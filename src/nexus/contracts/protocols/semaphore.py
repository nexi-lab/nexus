"""VFS Semaphore protocol — structural interface for counting semaphores."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class VFSSemaphoreProtocol(Protocol):
    """Structural interface shared by Rust and Python implementations."""

    def acquire(
        self,
        name: str,
        max_holders: int,
        timeout_ms: int = 0,
        ttl_ms: int = 30_000,
    ) -> str | None: ...

    def release(self, name: str, holder_id: str) -> bool: ...

    def extend(self, name: str, holder_id: str, ttl_ms: int = 30_000) -> bool: ...

    def info(self, name: str) -> dict | None: ...

    def force_release(self, name: str) -> bool: ...

    def stats(self) -> dict: ...

    @property
    def active_semaphores(self) -> int: ...
