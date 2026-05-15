"""RemoteZoneBackend — permission-aware proxy backend for federated hub zones.

Subclasses RemoteBackend to add per-zone read/write permission enforcement.
A zone with permission="r" raises ZoneReadOnlyError before any gRPC call
for write operations. A zone with permission="rw" passes all ops through.

Issue #3786: Local workspace + remote hub federation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from nexus.backends.storage.remote import RemoteBackend
from nexus.contracts.exceptions import ZoneReadOnlyError

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.core.object_store import WriteResult
    from nexus.remote.rpc_transport import RPCTransport


class RemoteZoneBackend(RemoteBackend):
    """RemoteBackend with per-zone permission enforcement.

    Args:
        zone_id: Zone identifier (used in error messages and backend name).
        transport: Shared RPCTransport instance connected to the hub.
        permission: "r" for read-only, "rw" for read-write.
    """

    def __init__(
        self,
        zone_id: str,
        transport: "RPCTransport",
        permission: Literal["r", "rw"],
    ) -> None:
        super().__init__(transport)
        self.zone_id = zone_id
        self.permission = permission

    @property
    def name(self) -> str:
        return f"remote_zone:{self.zone_id}"

    def _check_write_permission(self) -> None:
        if self.permission != "rw":
            raise ZoneReadOnlyError(
                f"Zone '{self.zone_id}' is read-only (permission='{self.permission}')"
            )

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> "WriteResult":
        self._check_write_permission()
        return super().write_content(content, content_id, offset=offset, context=context)

    def delete_content(self, content_id: str, context: "OperationContext | None" = None) -> None:
        self._check_write_permission()
        # RemoteBackend.delete_content is intentionally a no-op — it relies on
        # RemoteMetastore.delete to send the RPC server-side. In sandbox profile,
        # remote zones have no local metastore entries, so we call the transport
        # directly to ensure the delete reaches the hub.
        path = self._to_server_path(context)
        self._transport.delete_file(path)

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        self._check_write_permission()
        super().mkdir(path, parents=parents, exist_ok=exist_ok, context=context)

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        self._check_write_permission()
        super().rmdir(path, recursive=recursive, context=context)
