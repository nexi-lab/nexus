"""Portability brick contracts — tier-neutral protocols.

Defines the narrow NexusFS surface the portability brick needs,
avoiding direct imports from nexus.core.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MetastoreReadProtocol(Protocol):
    """Read-only metastore surface for export."""

    def list(self, prefix: str = "") -> Any:
        """List file metadata entries by prefix."""
        ...

    def get(self, path: str) -> Any | None:
        """Get file metadata for a path."""
        ...


@runtime_checkable
class MetastoreWriteProtocol(MetastoreReadProtocol, Protocol):
    """Read-write metastore surface for import."""

    def put(self, metadata: Any) -> None:
        """Store file metadata."""
        ...


@runtime_checkable
class ContentBackendProtocol(Protocol):
    """Narrow backend surface for reading content blobs."""

    def read_content(self, content_id: str) -> Any:
        """Read a content blob by its CAS hash."""
        ...


@runtime_checkable
class PortabilityFSProtocol(Protocol):
    """Narrow NexusFS surface used by the portability brick.

    Bricks must not import from nexus.core directly. This protocol
    defines the minimal surface the portability export/import services
    need from NexusFS.

    Bricks access content/metadata through these properties, not through
    the full NexusFilesystem syscall API.
    """

    @property
    def metadata(self) -> Any:
        """Metastore for reading/writing file metadata."""
        ...

    def sys_read(self, path: str, **kwargs: Any) -> bytes | None:
        """Read file content by path. R20.18.x routes through the
        kernel's mount LPM; replaced the `backend.read_content(hash)`
        direct access NexusFS no longer exposes."""
        ...

    def write(self, path: str, buf: bytes, **kwargs: Any) -> dict[str, Any]:
        """Write file content. Used by import_zone to restore blobs."""
        ...
