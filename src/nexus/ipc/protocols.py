"""Protocols (interfaces) for IPC brick dependencies.

The IPC brick depends on VFS and EventBus capabilities but does NOT
import from ``nexus.core`` directly. Instead, it defines minimal
Protocol interfaces here. The real implementations are injected at
wiring time (factory/builder).

This keeps the IPC brick testable in isolation — unit tests inject
in-memory fakes that satisfy these Protocols.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VFSOperations(Protocol):
    """Minimal VFS interface required by the IPC brick.

    A strict subset of VFSRouterProtocol — only the operations needed
    for inbox/outbox file management.
    """

    async def read(self, path: str, zone_id: str) -> bytes:
        """Read file contents at the given path."""
        ...

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        """Write data to the given path (create or overwrite)."""
        ...

    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        """List filenames in a directory (not full paths)."""
        ...

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        """Atomically rename/move a file from src to dst."""
        ...

    async def mkdir(self, path: str, zone_id: str) -> None:
        """Create a directory (including parents if needed)."""
        ...

    async def count_dir(self, path: str, zone_id: str) -> int:
        """Count entries in a directory without listing them.

        More efficient than ``len(await self.list_dir(...))``.

        Raises:
            FileNotFoundError: If the directory does not exist.
        """
        ...

    async def exists(self, path: str, zone_id: str) -> bool:
        """Check if a path exists."""
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """Minimal event publishing interface required by the IPC brick.

    Used to notify recipients of new messages. A subset of
    EventBusProtocol — only publish, not subscribe.
    """

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        """Publish an event to a channel."""
        ...


@runtime_checkable
class EventSubscriber(Protocol):
    """Minimal event subscription interface required by the IPC brick.

    Used by MessageProcessor to receive push notifications of new
    inbox messages.
    """

    async def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to events on a channel. Yields events as they arrive."""
        ...
