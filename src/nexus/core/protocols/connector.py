"""Storage connector protocol interfaces (Issue #1601).

Defines the Storage Brick boundary as composable protocols:

- ``ContentStoreProtocol`` — Minimal CAS interface (most consumers need only this)
- ``DirectoryOpsProtocol`` — Directory operations (VFS Router, mount services)
- ``ConnectorProtocol`` — Full connector interface (Storage Brick boundary)

Design decisions:
    - Protocol for brick interfaces, ABC for internal implementations (§11.3)
    - Protocols in ``core/protocols/``, implementations stay in ``backends/`` (§11.4)
    - Modeled after ``VFSRouterProtocol`` pattern (§5.1)

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §11.3, §11.4
    - Issue #1601: ConnectorProtocol + Storage Brick Extraction
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.backends.backend import HandlerStatusResponse
    from nexus.core.permissions import OperationContext
    from nexus.core.response import HandlerResponse


@runtime_checkable
class ContentStoreProtocol(Protocol):
    """Minimal CAS interface — most consumers need only this.

    Covers content-addressable storage operations: write, read, delete,
    existence check, size, and reference counting.
    """

    @property
    def name(self) -> str: ...

    def write_content(
        self, content: bytes, context: OperationContext | None = None
    ) -> HandlerResponse[str]: ...

    def read_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bytes]: ...

    def delete_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[None]: ...

    def content_exists(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]: ...

    def get_content_size(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]: ...

    def get_ref_count(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]: ...


@runtime_checkable
class DirectoryOpsProtocol(Protocol):
    """Directory operations — needed by VFS Router and mount services."""

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]: ...

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]: ...

    def is_directory(
        self, path: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]: ...


@runtime_checkable
class ConnectorProtocol(ContentStoreProtocol, DirectoryOpsProtocol, Protocol):
    """Full connector interface — the Storage Brick boundary.

    Combines CAS content operations, directory operations, and connection
    lifecycle management with capability flags for polymorphic dispatch.
    """

    # --- Connection lifecycle ---

    def connect(self, context: OperationContext | None = None) -> HandlerStatusResponse: ...

    def disconnect(self, context: OperationContext | None = None) -> None: ...

    def check_connection(
        self, context: OperationContext | None = None
    ) -> HandlerStatusResponse: ...

    # --- Capability flags ---

    @property
    def user_scoped(self) -> bool: ...

    @property
    def is_connected(self) -> bool: ...

    @property
    def is_passthrough(self) -> bool: ...

    @property
    def has_root_path(self) -> bool: ...

    @property
    def has_virtual_filesystem(self) -> bool: ...

    @property
    def has_token_manager(self) -> bool: ...
