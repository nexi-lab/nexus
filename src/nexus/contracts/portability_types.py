"""Portability brick contracts — tier-neutral protocols.

Defines the narrow NexusFS surface the portability brick needs,
avoiding direct imports from nexus.core.
"""

from __future__ import annotations

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

    def read_content(self, content_hash: str) -> Any:
        """Read a content blob by its CAS hash."""
        ...


@runtime_checkable
class ReBACPortabilityProtocol(Protocol):
    """Narrow ReBAC surface for portability permission export/import.

    Bricks must not reach into NexusFS via getattr to find the rebac_manager.
    Instead, callers inject an explicit rebac dependency that satisfies this protocol.
    """

    def get_zone_tuples(self, zone_id: str) -> list[dict[str, str]]:
        """Return all ReBAC tuples for a zone (used by export)."""
        ...

    def rebac_write(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        zone_id: str,
    ) -> None:
        """Write a single ReBAC tuple (used by import)."""
        ...


@runtime_checkable
class PortabilityFSProtocol(Protocol):
    """Narrow NexusFS surface used by the portability brick.

    Bricks must not import from nexus.core directly. This protocol
    defines the minimal surface the portability export/import services
    need from NexusFS.
    """

    @property
    def metadata(self) -> Any:
        """Metastore for reading/writing file metadata."""
        ...

    @property
    def backend(self) -> Any:
        """Backend for reading content blobs."""
        ...

    def write(
        self,
        path: str,
        content: bytes,
        context: Any = None,
        *,
        force: bool = False,
    ) -> Any:
        """Write content to a file."""
        ...
