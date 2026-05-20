from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from nexus.backends.engines.multipart import MultipartUpload
from nexus.bricks.upload.chunked_upload_service import ChunkedUploadConfig, ChunkedUploadService
from nexus.bricks.upload.upload_session import UploadStatus
from nexus.contracts.types import OperationContext
from tests.helpers.in_memory_record_store import InMemoryRecordStore


@dataclass(frozen=True)
class _WriteResult:
    content_id: str


class _MemoryBackend:
    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}
        self._next = 0

    def write_content(self, data: bytes) -> _WriteResult:
        self._next += 1
        content_id = f"content-{self._next}"
        self._content[content_id] = data
        return _WriteResult(content_id=content_id)

    def read_content(self, content_id: str) -> bytes:
        return self._content[content_id]


class _MetadataStore:
    def __init__(self) -> None:
        self.writes: list[Any] = []

    def put(self, metadata: Any) -> None:
        self.writes.append(metadata)


class _MultipartMemoryBackend(_MemoryBackend, MultipartUpload):
    def __init__(self) -> None:
        super().__init__()
        self.init_calls = 0
        self.uploaded_parts: list[bytes] = []
        self.completed = False

    def init_multipart(
        self,
        backend_path: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> str:
        self.init_calls += 1
        return "multipart-upload"

    def upload_part(
        self,
        backend_path: str,
        upload_id: str,
        part_number: int,
        data: bytes,
    ) -> dict[str, Any]:
        self.uploaded_parts.append(data)
        return {"etag": f"part-{part_number}", "part_number": part_number}

    def complete_multipart(
        self,
        backend_path: str,
        upload_id: str,
        parts: list[dict[str, Any]],
    ) -> str:
        self.completed = True
        return "multipart-content"

    def abort_multipart(self, backend_path: str, upload_id: str) -> None:
        pass


class _NexusFS:
    """In-memory NexusFS fake exposing the §2.5 syscall surface.

    Chunked upload stages chunks via sys_write under /__sys__/chunked-uploads/,
    reassembles via sys_read, and writes the final user-facing file via
    write(). sys_unlink clears the staging subtree.
    """

    def __init__(self, *, fail_final_write_once: bool = False) -> None:
        self.writes: list[dict[str, Any]] = []
        self._paths: dict[str, bytes] = {}
        self._fail_final_write_once = fail_final_write_once
        self._final_write_calls = 0

    def write(
        self,
        path: str,
        buf: bytes,
        *,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        self._final_write_calls += 1
        if self._fail_final_write_once and self._final_write_calls == 1:
            raise RuntimeError("final write failed")
        self.writes.append({"path": path, "buf": bytes(buf), "context": context})
        return {
            "content_id": "fs-content",
            "version": 1,
            "modified_at": None,
            "size": len(buf),
        }

    def sys_write(
        self,
        path: str,
        buf: bytes,
        *,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        self._paths[path] = bytes(buf)
        return {"size": len(buf)}

    def sys_read(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
    ) -> bytes:
        if path not in self._paths:
            raise FileNotFoundError(path)
        return self._paths[path]

    def sys_unlink(
        self,
        path: str,
        *,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        prefix = path.rstrip("/") + "/"
        for key in list(self._paths):
            if key == path or key.startswith(prefix):
                del self._paths[key]
        return {}


def _service(
    record_store: InMemoryRecordStore,
    backend: _MemoryBackend,
    metadata_store: _MetadataStore | None = None,
    *,
    nexus_fs: _NexusFS | None = None,
) -> ChunkedUploadService:
    kwargs: dict[str, Any] = {}
    if nexus_fs is not None:
        kwargs["nexus_fs"] = nexus_fs
    return ChunkedUploadService(
        record_store=record_store,
        backend=backend,
        metadata_store=metadata_store,
        config=ChunkedUploadConfig(min_chunk_size=1, max_chunk_size=1024),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_resume_after_service_restart_uses_persisted_part_metadata() -> None:
    record_store = InMemoryRecordStore()
    backend = _MemoryBackend()
    metadata_store = _MetadataStore()
    # The restarted service shares the same NexusFS handle — staged chunks
    # under /__sys__/chunked-uploads/ survive the "restart", same as a real
    # persistent kernel namespace would.
    nexus_fs = _NexusFS()
    first_service = _service(record_store, backend, metadata_store, nexus_fs=nexus_fs)

    upload = await first_service.create_upload("/uploads/restarted.txt", upload_length=10)
    await first_service.receive_chunk(upload.upload_id, 0, b"hello")

    restarted_service = _service(record_store, backend, metadata_store, nexus_fs=nexus_fs)
    completed = await restarted_service.receive_chunk(upload.upload_id, 5, b"world")

    assert completed.content_id == "fs-content"
    # The final assembled file is written through the syscall surface.
    assert nexus_fs.writes[-1]["path"] == "/uploads/restarted.txt"
    assert nexus_fs.writes[-1]["buf"] == b"helloworld"


@pytest.mark.asyncio
async def test_completed_upload_uses_attached_filesystem_write_path() -> None:
    record_store = InMemoryRecordStore()
    backend = _MemoryBackend()
    metadata_store = _MetadataStore()
    nexus_fs = _NexusFS()
    service = _service(record_store, backend, metadata_store, nexus_fs=nexus_fs)
    context = OperationContext(user_id="alice", groups=[])

    upload = await service.create_upload(
        "/uploads/through-fs.txt",
        upload_length=4,
        user_id="alice",
    )
    completed = await service.receive_chunk(upload.upload_id, 0, b"data", context=context)

    assert completed.content_id == "fs-content"
    assert nexus_fs.writes == [
        {"path": "/uploads/through-fs.txt", "buf": b"data", "context": context}
    ]
    assert metadata_store.writes == []


@pytest.mark.asyncio
async def test_attached_filesystem_disables_backend_multipart_path() -> None:
    record_store = InMemoryRecordStore()
    backend = _MultipartMemoryBackend()
    nexus_fs = _NexusFS()
    service = _service(record_store, backend, nexus_fs=nexus_fs)

    upload = await service.create_upload("/uploads/multipart.txt", upload_length=4)
    completed = await service.receive_chunk(upload.upload_id, 0, b"data")

    assert upload.backend_upload_id is None
    assert completed.content_id == "fs-content"
    assert backend.init_calls == 0
    assert backend.uploaded_parts == []
    assert not backend.completed


@pytest.mark.asyncio
async def test_final_chunk_failure_keeps_upload_retryable() -> None:
    record_store = InMemoryRecordStore()
    backend = _MemoryBackend()
    metadata_store = _MetadataStore()
    # The final assembled write (nexus_fs.write) fails on its first call;
    # the upload must stay retryable and succeed on the second attempt.
    nexus_fs = _NexusFS(fail_final_write_once=True)
    service = _service(record_store, backend, metadata_store, nexus_fs=nexus_fs)

    upload = await service.create_upload("/uploads/retryable.txt", upload_length=4)

    with pytest.raises(RuntimeError, match="final write failed"):
        await service.receive_chunk(upload.upload_id, 0, b"data")

    persisted = await service.get_upload_status(upload.upload_id)
    assert persisted.upload_offset == 0
    assert persisted.status == UploadStatus.CREATED

    completed = await service.receive_chunk(upload.upload_id, 0, b"data")
    assert completed.status == UploadStatus.COMPLETED
    assert completed.content_id == "fs-content"
    assert nexus_fs.writes[-1]["buf"] == b"data"
