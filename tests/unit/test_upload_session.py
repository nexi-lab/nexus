"""Unit tests for UploadSession dataclass (Issue #788)."""

from datetime import UTC, datetime, timedelta

import pytest

from nexus.services.upload_session import UploadSession, UploadStatus


class TestUploadStatus:
    """Tests for UploadStatus enum."""

    def test_all_values_exist(self) -> None:
        assert UploadStatus.CREATED == "created"
        assert UploadStatus.IN_PROGRESS == "in_progress"
        assert UploadStatus.COMPLETED == "completed"
        assert UploadStatus.TERMINATED == "terminated"
        assert UploadStatus.EXPIRED == "expired"

    def test_from_string(self) -> None:
        assert UploadStatus("created") == UploadStatus.CREATED
        assert UploadStatus("in_progress") == UploadStatus.IN_PROGRESS


class TestUploadSession:
    """Tests for UploadSession dataclass."""

    def test_create_with_defaults(self) -> None:
        session = UploadSession(
            upload_id="test-123",
            target_path="/data/file.txt",
            upload_length=1024,
        )
        assert session.upload_id == "test-123"
        assert session.target_path == "/data/file.txt"
        assert session.upload_length == 1024
        assert session.upload_offset == 0
        assert session.status == UploadStatus.CREATED
        assert session.zone_id == "default"
        assert session.user_id == "anonymous"
        assert session.metadata == {}
        assert session.checksum_algorithm is None
        assert session.expires_at is None
        assert session.backend_upload_id is None
        assert session.parts_received == 0
        assert session.content_hash is None

    def test_immutability(self) -> None:
        session = UploadSession(
            upload_id="test-123",
            target_path="/data/file.txt",
            upload_length=1024,
        )
        with pytest.raises(AttributeError):
            session.upload_offset = 512  # type: ignore[misc]

    def test_to_dict_round_trip(self) -> None:
        now = datetime.now(UTC)
        expires = now + timedelta(hours=24)
        session = UploadSession(
            upload_id="abc-def",
            target_path="/workspace/report.pdf",
            upload_length=10_000_000,
            upload_offset=5_000_000,
            status=UploadStatus.IN_PROGRESS,
            zone_id="zone-1",
            user_id="user-42",
            metadata={"filename": "report.pdf", "content_type": "application/pdf"},
            checksum_algorithm="sha256",
            created_at=now,
            expires_at=expires,
            backend_upload_id="s3-upload-xyz",
            backend_name="s3_connector",
            parts_received=5,
            content_hash=None,
        )

        data = session.to_dict()
        restored = UploadSession.from_dict(data)

        assert restored.upload_id == session.upload_id
        assert restored.target_path == session.target_path
        assert restored.upload_length == session.upload_length
        assert restored.upload_offset == session.upload_offset
        assert restored.status == session.status
        assert restored.zone_id == session.zone_id
        assert restored.user_id == session.user_id
        assert restored.metadata == session.metadata
        assert restored.checksum_algorithm == session.checksum_algorithm
        assert restored.backend_upload_id == session.backend_upload_id
        assert restored.backend_name == session.backend_name
        assert restored.parts_received == session.parts_received

    def test_to_dict_serializes_status(self) -> None:
        session = UploadSession(
            upload_id="test",
            target_path="/f",
            upload_length=100,
            status=UploadStatus.COMPLETED,
        )
        data = session.to_dict()
        assert data["status"] == "completed"

    def test_from_dict_minimal(self) -> None:
        data = {
            "upload_id": "min-test",
            "target_path": "/min",
            "upload_length": 42,
        }
        session = UploadSession.from_dict(data)
        assert session.upload_id == "min-test"
        assert session.upload_offset == 0
        assert session.status == UploadStatus.CREATED

    def test_from_dict_with_none_timestamps(self) -> None:
        data = {
            "upload_id": "ts-test",
            "target_path": "/ts",
            "upload_length": 100,
            "created_at": None,
            "expires_at": None,
        }
        session = UploadSession.from_dict(data)
        assert session.created_at is not None  # defaults to now
        assert session.expires_at is None

    def test_is_complete_property(self) -> None:
        incomplete = UploadSession(
            upload_id="ic",
            target_path="/f",
            upload_length=1000,
            upload_offset=500,
        )
        assert not incomplete.is_complete

        complete = UploadSession(
            upload_id="c",
            target_path="/f",
            upload_length=1000,
            upload_offset=1000,
        )
        assert complete.is_complete

    def test_is_complete_zero_length(self) -> None:
        session = UploadSession(
            upload_id="zero",
            target_path="/f",
            upload_length=0,
            upload_offset=0,
        )
        assert session.is_complete

    def test_is_expired_property(self) -> None:
        not_expired = UploadSession(
            upload_id="ne",
            target_path="/f",
            upload_length=100,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        assert not not_expired.is_expired

        expired = UploadSession(
            upload_id="e",
            target_path="/f",
            upload_length=100,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        assert expired.is_expired

    def test_is_expired_no_expiry(self) -> None:
        session = UploadSession(
            upload_id="no-exp",
            target_path="/f",
            upload_length=100,
            expires_at=None,
        )
        assert not session.is_expired

    def test_remaining_bytes(self) -> None:
        session = UploadSession(
            upload_id="rb",
            target_path="/f",
            upload_length=1000,
            upload_offset=300,
        )
        assert session.remaining_bytes == 700

    def test_remaining_bytes_complete(self) -> None:
        session = UploadSession(
            upload_id="rbc",
            target_path="/f",
            upload_length=1000,
            upload_offset=1000,
        )
        assert session.remaining_bytes == 0

    def test_metadata_default_factory_isolation(self) -> None:
        s1 = UploadSession(upload_id="1", target_path="/a", upload_length=10)
        s2 = UploadSession(upload_id="2", target_path="/b", upload_length=20)
        assert s1.metadata is not s2.metadata
