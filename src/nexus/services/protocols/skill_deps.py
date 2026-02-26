"""Narrow protocol dependencies for the Skills brick (Issue #2035).

Replaces the broad NexusFSGateway dependency with 2 narrow protocols
that describe exactly what the Skills brick needs:

- SkillFilesystemProtocol: filesystem operations (sys_read, sys_write, sys_mkdir, sys_readdir, sys_access)
- SkillPermissionProtocol: ReBAC permission operations (check, create, list, delete)

These protocols allow the Skills brick to be tested in isolation with
in-memory fakes (see skills/testing.py).
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SkillFilesystemProtocol(Protocol):
    """Filesystem operations needed by the Skills brick."""

    def sys_read(self, path: str, *, context: Any = None) -> bytes | str:
        """Read file content (POSIX read)."""
        ...

    def sys_write(self, path: str, content: bytes | str, *, context: Any = None) -> None:
        """Write content to a file (POSIX write)."""
        ...

    def sys_mkdir(self, path: str, *, context: Any = None) -> None:
        """Create a directory (POSIX mkdir)."""
        ...

    def sys_readdir(self, path: str, *, context: Any = None) -> list[str]:
        """List files in a directory (POSIX readdir)."""
        ...

    def sys_access(self, path: str, *, context: Any = None) -> bool:
        """Check if a path exists (POSIX access)."""
        ...


@runtime_checkable
class SkillPermissionProtocol(Protocol):
    """Permission operations needed by the Skills brick."""

    def rebac_check(
        self,
        *,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> bool:
        """Check if subject has permission on object."""
        ...

    def rebac_create(
        self,
        *,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        context: Any = None,
    ) -> dict[str, Any] | None:
        """Create a ReBAC permission tuple."""
        ...

    def rebac_list_tuples(
        self,
        *,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """List ReBAC tuples matching filters."""
        ...

    def rebac_delete_object_tuples(
        self,
        *,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> int:
        """Delete all tuples for an object."""
        ...

    def invalidate_metadata_cache(self, *paths: str) -> None:
        """Invalidate metadata cache entries."""
        ...

    @property
    def rebac_manager(self) -> Any:
        """Access the underlying ReBAC manager for direct operations."""
        ...
