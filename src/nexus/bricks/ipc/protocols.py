"""Protocols (interfaces) for IPC brick dependencies.

The IPC brick depends on EventBus capabilities and a pluggable storage
driver (``VFSOperations``) but does NOT import from ``nexus.core``
directly. It defines minimal Protocol interfaces here for event
publishing/subscribing. The real implementations are injected at wiring
time (factory/builder).

``VFSOperations`` is the canonical storage protocol used by all IPC
components (delivery, sweep, discovery, provisioning). The production
implementation is ``KernelVFSAdapter`` which routes through NexusFS.

This keeps the IPC brick testable in isolation — unit tests inject
in-memory fakes that satisfy these Protocols.
"""

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VFSOperations(Protocol):
    """Minimal VFS interface required by the IPC brick.

    A strict subset of VFSRouterProtocol — only the operations needed
    for inbox/outbox file management.
    """

    async def sys_read(self, path: str, zone_id: str) -> bytes:
        """Read file contents at the given path."""
        ...

    async def sys_write(self, path: str, data: bytes, zone_id: str) -> None:
        """Write data to the given path (create or overwrite)."""
        ...

    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        """List filenames in a directory (not full paths)."""
        ...

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        """Atomically rename/move a file from src to dst."""
        ...

    async def sys_mkdir(self, path: str, zone_id: str) -> None:
        """Create a directory (including parents if needed)."""
        ...

    async def count_dir(self, path: str, zone_id: str) -> int:
        """Count entries in a directory without listing them.

        More efficient than ``len(await self.list_dir(...))``.

        Raises:
            FileNotFoundError: If the directory does not exist.
        """
        ...

    async def sys_access(self, path: str, zone_id: str) -> bool:
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

    def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to events on a channel. Yields events as they arrive."""
        ...


# ---------------------------------------------------------------------------
# Protocols for cross-zone routing (replaces services.protocols imports)
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentInfoResult(Protocol):
    """Minimal result from agent registry lookup."""

    @property
    def zone_id(self) -> str | None: ...


@runtime_checkable
class AgentLookupProtocol(Protocol):
    """Minimal agent registry interface for zone resolution.

    Satisfied by AgentRegistry or compatible lookup at wiring time.
    """

    async def get(self, agent_id: str) -> AgentInfoResult | None:
        """Look up agent info by ID. Returns None if not found."""
        ...


@runtime_checkable
class PermissionCheckProtocol(Protocol):
    """Minimal ReBAC permission check interface.

    Satisfied by the real ``PermissionProtocol`` at wiring time.
    """

    async def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
    ) -> bool:
        """Check if subject has permission on object."""
        ...


# ---------------------------------------------------------------------------
# Protocols for message signing (replaces identity.* concrete imports)
# ---------------------------------------------------------------------------


class KeyRecord(Protocol):
    """Minimal key record returned by KeyServiceProtocol."""

    @property
    def key_id(self) -> str: ...

    @property
    def did(self) -> str: ...

    @property
    def is_active(self) -> bool: ...

    @property
    def revoked_at(self) -> Any | None: ...

    @property
    def expires_at(self) -> Any | None: ...

    @property
    def public_key_bytes(self) -> bytes: ...


@runtime_checkable
class KeyServiceProtocol(Protocol):
    """Minimal key management interface for IPC signing.

    Used by ``MessageSigner`` and ``MessageVerifier``.
    Satisfied by the real ``KeyService`` at wiring time.
    """

    def ensure_keypair(self, agent_id: str) -> KeyRecord:
        """Provision or retrieve a keypair for the agent."""
        ...

    def get_public_key(self, key_id: str) -> KeyRecord | None:
        """Retrieve public key record by key ID."""
        ...

    def decrypt_private_key(self, key_id: str) -> Any:
        """Decrypt and return the private key object."""
        ...


@runtime_checkable
class CryptoProtocol(Protocol):
    """Minimal cryptographic operations interface for IPC signing.

    Used by ``MessageSigner`` and ``MessageVerifier``.
    Satisfied by the real ``IdentityCrypto`` at wiring time.
    """

    def sign(self, data: bytes, private_key: Any) -> bytes:
        """Sign data with the given private key."""
        ...

    def verify(self, data: bytes, signature: bytes, public_key: Any) -> bool:
        """Verify a signature against data and public key."""
        ...

    @staticmethod
    def public_key_from_bytes(key_bytes: bytes) -> Any:
        """Reconstruct a public key object from raw bytes."""
        ...
