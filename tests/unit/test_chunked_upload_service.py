"""Unit tests for ChunkedUploadService (Issue #788)."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.core.exceptions import (
    UploadChecksumMismatchError,
    UploadExpiredError,
    UploadNotFoundError,
    UploadOffsetMismatchError,
    ValidationError,
)
from nexus.services.chunked_upload_service import (
    ChunkedUploadConfig,
    ChunkedUploadService,
)
from nexus.services.upload_session import UploadStatus
from nexus.storage.models.upload_session import UploadSessionModel

# --- Fixtures ---


class FakeSessionContext:
    """Minimal fake SQLAlchemy session for testing."""

    def __init__(self) -> None:
        self._store: dict[str, UploadSessionModel] = {}

    def query(self, model: type) -> FakeSessionContext:
        self._query_model = model
        return self

    def filter_by(self, **kwargs: Any) -> FakeSessionContext:
        self._filter_kwargs = kwargs
        return self

    def filter(self, *args: Any) -> FakeSessionContext:
        return self

    def all(self) -> list[UploadSessionModel]:
        return list(self._store.values())

    def first(self) -> UploadSessionModel | None:
        uid = self._filter_kwargs.get("upload_id")
        return self._store.get(uid) if uid else None

    def add(self, model: UploadSessionModel) -> None:
        self._store[model.upload_id] = model

    def delete(self) -> None:
        uid = self._filter_kwargs.get("upload_id")
        if uid:
            self._store.pop(uid, None)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def fake_db() -> FakeSessionContext:
    return FakeSessionContext()


@pytest.fixture
def mock_backend() -> MagicMock:
    from nexus.core.response import HandlerResponse

    backend = MagicMock()
    backend.name = "mock"
    backend.write_content.return_value = HandlerResponse.ok(data="fakehash123")
    backend.read_content.return_value = HandlerResponse.ok(data=b"chunk_data")
    return backend


@pytest.fixture
def config() -> ChunkedUploadConfig:
    return ChunkedUploadConfig(
        max_concurrent_uploads=3,
        session_ttl_hours=1,
        cleanup_interval_seconds=60,
        min_chunk_size=10,  # small for testing
        max_chunk_size=1024 * 1024,  # 1MB
        default_chunk_size=100,
        max_upload_size=10 * 1024 * 1024,  # 10MB
    )


@pytest.fixture
def service(fake_db: FakeSessionContext, mock_backend: MagicMock, config: ChunkedUploadConfig) -> ChunkedUploadService:
    return ChunkedUploadService(
        session_factory=lambda: fake_db,
        backend=mock_backend,
        config=config,
    )


# --- Tests ---


class TestCreateUpload:
    @pytest.mark.asyncio
    async def test_create_upload_basic(self, service: ChunkedUploadService) -> None:
        session = await service.create_upload(
            target_path="/data/file.txt",
            upload_length=1024,
        )
        assert session.upload_id
        assert session.target_path == "/data/file.txt"
        assert session.upload_length == 1024
        assert session.upload_offset == 0
        assert session.status == UploadStatus.CREATED
        assert session.expires_at is not None

    @pytest.mark.asyncio
    async def test_create_upload_with_metadata(self, service: ChunkedUploadService) -> None:
        session = await service.create_upload(
            target_path="/data/file.txt",
            upload_length=1024,
            metadata={"filename": "test.txt", "content_type": "text/plain"},
            zone_id="zone-1",
            user_id="user-42",
        )
        assert session.metadata["filename"] == "test.txt"
        assert session.zone_id == "zone-1"
        assert session.user_id == "user-42"

    @pytest.mark.asyncio
    async def test_create_upload_validates_upload_length_negative(
        self, service: ChunkedUploadService
    ) -> None:
        with pytest.raises(ValidationError, match="non-negative"):
            await service.create_upload(target_path="/f", upload_length=-1)

    @pytest.mark.asyncio
    async def test_create_upload_validates_upload_length_too_large(
        self, service: ChunkedUploadService
    ) -> None:
        with pytest.raises(ValidationError, match="exceeds maximum"):
            await service.create_upload(
                target_path="/f",
                upload_length=100 * 1024 * 1024 * 1024,  # 100 GB
            )

    @pytest.mark.asyncio
    async def test_create_upload_returns_429_at_capacity(
        self,
        fake_db: FakeSessionContext,
        mock_backend: MagicMock,
    ) -> None:
        config = ChunkedUploadConfig(max_concurrent_uploads=1)
        svc = ChunkedUploadService(
            session_factory=lambda: fake_db,
            backend=mock_backend,
            config=config,
        )
        # First upload succeeds
        await svc.create_upload(target_path="/f1", upload_length=100)
        # Second upload hits limit
        with pytest.raises(RuntimeError, match="Too many concurrent"):
            await svc.create_upload(target_path="/f2", upload_length=100)


class TestReceiveChunk:
    @pytest.mark.asyncio
    async def test_receive_chunk_single(self, service: ChunkedUploadService) -> None:
        session = await service.create_upload(target_path="/f", upload_length=50)

        updated = await service.receive_chunk(
            upload_id=session.upload_id,
            offset=0,
            chunk_data=b"x" * 50,
        )
        assert updated.upload_offset == 50
        assert updated.status == UploadStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_receive_chunk_multi(self, service: ChunkedUploadService) -> None:
        session = await service.create_upload(target_path="/f", upload_length=100)

        updated = await service.receive_chunk(
            upload_id=session.upload_id,
            offset=0,
            chunk_data=b"x" * 50,
        )
        assert updated.upload_offset == 50
        assert updated.status == UploadStatus.IN_PROGRESS

        updated = await service.receive_chunk(
            upload_id=session.upload_id,
            offset=50,
            chunk_data=b"y" * 50,
        )
        assert updated.upload_offset == 100
        assert updated.status == UploadStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_receive_chunk_out_of_order(self, service: ChunkedUploadService) -> None:
        session = await service.create_upload(target_path="/f", upload_length=100)

        with pytest.raises(UploadOffsetMismatchError) as exc_info:
            await service.receive_chunk(
                upload_id=session.upload_id,
                offset=50,  # wrong offset
                chunk_data=b"x" * 50,
            )
        assert exc_info.value.expected_offset == 0
        assert exc_info.value.received_offset == 50

    @pytest.mark.asyncio
    async def test_receive_chunk_checksum_sha256(self, service: ChunkedUploadService) -> None:
        session = await service.create_upload(target_path="/f", upload_length=20)
        data = b"hello world 12345678"
        digest_b64 = base64.b64encode(hashlib.sha256(data).digest()).decode()
        checksum_header = f"sha256 {digest_b64}"

        updated = await service.receive_chunk(
            upload_id=session.upload_id,
            offset=0,
            chunk_data=data,
            checksum_header=checksum_header,
        )
        assert updated.upload_offset == 20

    @pytest.mark.asyncio
    async def test_receive_chunk_checksum_mismatch(self, service: ChunkedUploadService) -> None:
        session = await service.create_upload(target_path="/f", upload_length=20)
        data = b"hello world 12345678"
        wrong_digest = base64.b64encode(b"wrong" * 6).decode()
        checksum_header = f"sha256 {wrong_digest}"

        with pytest.raises(UploadChecksumMismatchError):
            await service.receive_chunk(
                upload_id=session.upload_id,
                offset=0,
                chunk_data=data,
                checksum_header=checksum_header,
            )

    @pytest.mark.asyncio
    async def test_receive_chunk_checksum_md5(self, service: ChunkedUploadService) -> None:
        session = await service.create_upload(target_path="/f", upload_length=20)
        data = b"hello world 12345678"
        digest_b64 = base64.b64encode(hashlib.md5(data).digest()).decode()
        checksum_header = f"md5 {digest_b64}"

        updated = await service.receive_chunk(
            upload_id=session.upload_id,
            offset=0,
            chunk_data=data,
            checksum_header=checksum_header,
        )
        assert updated.upload_offset == 20

    @pytest.mark.asyncio
    async def test_receive_chunk_expired_session(
        self, fake_db: FakeSessionContext, mock_backend: MagicMock
    ) -> None:
        config = ChunkedUploadConfig(session_ttl_hours=0)  # immediate expiry
        svc = ChunkedUploadService(
            session_factory=lambda: fake_db,
            backend=mock_backend,
            config=config,
        )
        session = await svc.create_upload(target_path="/f", upload_length=100)

        # Manually expire the session in the DB
        model = fake_db._store[session.upload_id]
        model.expires_at = datetime.now(UTC) - timedelta(hours=1)

        with pytest.raises(UploadExpiredError):
            await svc.receive_chunk(
                upload_id=session.upload_id,
                offset=0,
                chunk_data=b"x" * 50,
            )

    @pytest.mark.asyncio
    async def test_receive_chunk_not_found(self, service: ChunkedUploadService) -> None:
        with pytest.raises(UploadNotFoundError):
            await service.receive_chunk(
                upload_id="nonexistent",
                offset=0,
                chunk_data=b"data",
            )

    @pytest.mark.asyncio
    async def test_receive_chunk_exceeds_upload_length(self, service: ChunkedUploadService) -> None:
        session = await service.create_upload(target_path="/f", upload_length=50)

        with pytest.raises(ValidationError, match="exceed upload_length"):
            await service.receive_chunk(
                upload_id=session.upload_id,
                offset=0,
                chunk_data=b"x" * 100,  # bigger than upload_length
            )

    @pytest.mark.asyncio
    async def test_receive_chunk_oversized(
        self, fake_db: FakeSessionContext, mock_backend: MagicMock
    ) -> None:
        config = ChunkedUploadConfig(max_chunk_size=100, min_chunk_size=10)
        svc = ChunkedUploadService(
            session_factory=lambda: fake_db,
            backend=mock_backend,
            config=config,
        )
        session = await svc.create_upload(target_path="/f", upload_length=10000)

        with pytest.raises(ValidationError, match="exceeds maximum"):
            await svc.receive_chunk(
                upload_id=session.upload_id,
                offset=0,
                chunk_data=b"x" * 200,  # bigger than max_chunk_size
            )


class TestGetUploadStatus:
    @pytest.mark.asyncio
    async def test_get_upload_status(self, service: ChunkedUploadService) -> None:
        session = await service.create_upload(target_path="/f", upload_length=100)
        status = await service.get_upload_status(session.upload_id)
        assert status.upload_id == session.upload_id
        assert status.upload_offset == 0

    @pytest.mark.asyncio
    async def test_get_upload_status_not_found(self, service: ChunkedUploadService) -> None:
        with pytest.raises(UploadNotFoundError):
            await service.get_upload_status("nonexistent")


class TestTerminateUpload:
    @pytest.mark.asyncio
    async def test_terminate_upload(self, service: ChunkedUploadService) -> None:
        session = await service.create_upload(target_path="/f", upload_length=100)
        await service.terminate_upload(session.upload_id)

        # Verify session is terminated
        status = await service.get_upload_status(session.upload_id)
        assert status.status == UploadStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_terminate_not_found(self, service: ChunkedUploadService) -> None:
        with pytest.raises(UploadNotFoundError):
            await service.terminate_upload("nonexistent")


class TestServerCapabilities:
    def test_get_server_capabilities(self, service: ChunkedUploadService) -> None:
        caps = service.get_server_capabilities()
        assert caps["Tus-Resumable"] == "1.0.0"
        assert caps["Tus-Version"] == "1.0.0"
        assert "creation" in caps["Tus-Extension"]
        assert "termination" in caps["Tus-Extension"]
        assert "checksum" in caps["Tus-Extension"]
        assert "expiration" in caps["Tus-Extension"]
        assert "sha256" in caps["Tus-Checksum-Algorithm"]
        assert "md5" in caps["Tus-Checksum-Algorithm"]
        assert "crc32" in caps["Tus-Checksum-Algorithm"]


class TestCleanupExpired:
    @pytest.mark.asyncio
    async def test_cleanup_expired_sweeps(
        self, fake_db: FakeSessionContext, mock_backend: MagicMock
    ) -> None:
        config = ChunkedUploadConfig(session_ttl_hours=0)
        svc = ChunkedUploadService(
            session_factory=lambda: fake_db,
            backend=mock_backend,
            config=config,
        )

        # Create a session that is already expired
        session = await svc.create_upload(target_path="/f", upload_length=100)
        model = fake_db._store[session.upload_id]
        model.expires_at = datetime.now(UTC) - timedelta(hours=1)

        cleaned = await svc.cleanup_expired()
        assert cleaned == 1


class TestChecksumVerification:
    def test_verify_sha256(self) -> None:
        data = b"test data"
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
        # Should not raise
        ChunkedUploadService._verify_checksum("test", data, f"sha256 {digest}")

    def test_verify_md5(self) -> None:
        data = b"test data"
        digest = base64.b64encode(hashlib.md5(data).digest()).decode()
        ChunkedUploadService._verify_checksum("test", data, f"md5 {digest}")

    def test_verify_crc32(self) -> None:
        import zlib

        data = b"test data"
        crc = zlib.crc32(data) & 0xFFFFFFFF
        digest = base64.b64encode(crc.to_bytes(4, "big")).decode()
        ChunkedUploadService._verify_checksum("test", data, f"crc32 {digest}")

    def test_verify_mismatch(self) -> None:
        data = b"test data"
        wrong_digest = base64.b64encode(b"wrong" * 6).decode()
        with pytest.raises(UploadChecksumMismatchError):
            ChunkedUploadService._verify_checksum("test", data, f"sha256 {wrong_digest}")

    def test_verify_unsupported_algorithm(self) -> None:
        with pytest.raises(ValidationError, match="Unsupported"):
            ChunkedUploadService._verify_checksum("test", b"data", "blake2b abc123")

    def test_verify_invalid_header_format(self) -> None:
        with pytest.raises(ValidationError, match="Invalid"):
            ChunkedUploadService._verify_checksum("test", b"data", "nospace")
