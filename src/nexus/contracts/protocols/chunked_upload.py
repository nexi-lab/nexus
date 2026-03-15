"""ChunkedUploadService protocol (Issue #696).

Defines the contract for tus.io resumable chunked uploads.

Existing implementation: ``nexus.bricks.upload.chunked_upload_service.ChunkedUploadService``

References:
    - docs/design/KERNEL-ARCHITECTURE.md §1 (service DI)
"""

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.bricks.upload.upload_session import UploadSession


@runtime_checkable
class ChunkedUploadProtocol(Protocol):
    """Service contract for tus.io resumable chunked uploads."""

    async def create_upload(
        self,
        target_path: str,
        upload_length: int,
        *,
        metadata: dict[str, str] | None = None,
        zone_id: str = ROOT_ZONE_ID,
        user_id: str = "anonymous",
        checksum_algorithm: str | None = None,
    ) -> "UploadSession": ...

    async def receive_chunk(
        self,
        upload_id: str,
        offset: int,
        chunk_data: bytes,
        checksum_header: str | None = None,
    ) -> "UploadSession": ...

    async def get_upload_status(self, upload_id: str) -> "UploadSession": ...

    async def terminate_upload(self, upload_id: str) -> None: ...

    async def cleanup_expired(self) -> int: ...

    async def start_cleanup_loop(self) -> None: ...

    def get_server_capabilities(self) -> dict[str, str]: ...
