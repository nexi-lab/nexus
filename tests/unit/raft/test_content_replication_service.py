"""Unit tests for ContentReplicationService."""

import json
from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.backend_address import BackendAddress
from nexus.contracts.metadata import FileMetadata
from nexus.raft.content_replication_service import ContentReplicationService
from nexus.raft.replication_policy import ReplicationPolicyResolver

SELF_ADDR = "10.0.0.1:50051"
REMOTE_ADDR = "10.0.0.2:50051"
REMOTE_ADDR_2 = "10.0.0.3:50051"


def _make_mount_fm(mount_point: str, replication: str | None = None) -> FileMetadata:
    data = {
        "mount_id": "m1",
        "mount_point": mount_point,
        "backend_type": "cas_local",
        "backend_config": {"path": "/data"},
        "replication": replication,
    }
    return FileMetadata(
        path=f"mnt:{mount_point}",
        backend_name="_mount_config",
        physical_path=json.dumps(data),
        size=0,
    )


def _make_file_meta(
    path: str,
    backend_name: str,
    size: int = 100,
    entry_type: int = 0,
) -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name=backend_name,
        physical_path="hash123",
        size=size,
        entry_type=entry_type,
    )


def _make_service(
    metastore: MagicMock | None = None,
    object_store: MagicMock | None = None,
    mount_entries: list[FileMetadata] | None = None,
    file_entries: list[FileMetadata] | None = None,
) -> ContentReplicationService:
    if metastore is None:
        metastore = MagicMock()

    # Configure metastore.list to return different results based on prefix
    if mount_entries is not None or file_entries is not None:
        _mount = mount_entries or []
        _files = file_entries or []

        def list_side_effect(prefix: str = "", recursive: bool = True, **kwargs):
            if prefix.startswith("mnt:"):
                return _mount
            return _files

        metastore.list.side_effect = list_side_effect

    if object_store is None:
        object_store = MagicMock()

    policy_resolver = ReplicationPolicyResolver(metastore)
    return ContentReplicationService(
        metastore=metastore,
        object_store=object_store,
        policy_resolver=policy_resolver,
        self_address=SELF_ADDR,
        scan_interval=1.0,
    )


class TestScanAndReplicate:
    """Test the synchronous _scan_and_replicate() method."""

    def test_no_replicated_prefixes_does_nothing(self):
        svc = _make_service(
            mount_entries=[_make_mount_fm("/data", replication=None)],
            file_entries=[],
        )
        svc._scan_and_replicate()
        # No errors, no object_store calls
        svc._object_store.write_content.assert_not_called()

    @patch.object(ContentReplicationService, "_fetch_from_peer")
    def test_replicates_remote_entry(self, mock_fetch):
        mock_fetch.return_value = b"file content"

        file_meta = _make_file_meta(
            "/shared/memory.md",
            f"local@{REMOTE_ADDR}",
        )
        svc = _make_service(
            mount_entries=[_make_mount_fm("/shared", replication="all-voters")],
            file_entries=[file_meta],
        )

        svc._scan_and_replicate()

        # Content was fetched and stored
        mock_fetch.assert_called_once_with(REMOTE_ADDR, "/shared/memory.md")
        svc._object_store.write_content.assert_called_once_with(b"file content")

        # backend_name was updated to include self
        put_call = svc._metastore.put.call_args[0][0]
        addr = BackendAddress.parse(put_call.backend_name)
        assert SELF_ADDR in addr.origins
        assert REMOTE_ADDR in addr.origins

    @patch.object(ContentReplicationService, "_fetch_from_peer")
    def test_skips_already_replicated(self, mock_fetch):
        """If self_address is already in origins, skip."""
        file_meta = _make_file_meta(
            "/shared/memory.md",
            f"local@{REMOTE_ADDR},{SELF_ADDR}",
        )
        svc = _make_service(
            mount_entries=[_make_mount_fm("/shared", replication="all-voters")],
            file_entries=[file_meta],
        )

        svc._scan_and_replicate()

        mock_fetch.assert_not_called()
        svc._object_store.write_content.assert_not_called()

    @patch.object(ContentReplicationService, "_fetch_from_peer")
    def test_skips_directories(self, mock_fetch):
        dir_meta = _make_file_meta(
            "/shared/subdir",
            f"local@{REMOTE_ADDR}",
            entry_type=1,  # DT_DIR
        )
        svc = _make_service(
            mount_entries=[_make_mount_fm("/shared", replication="all-voters")],
            file_entries=[dir_meta],
        )

        svc._scan_and_replicate()

        mock_fetch.assert_not_called()

    @patch.object(ContentReplicationService, "_fetch_from_peer")
    def test_skips_entries_without_origin(self, mock_fetch):
        file_meta = _make_file_meta("/shared/file.txt", "local")
        svc = _make_service(
            mount_entries=[_make_mount_fm("/shared", replication="all-voters")],
            file_entries=[file_meta],
        )

        svc._scan_and_replicate()

        mock_fetch.assert_not_called()

    @patch.object(ContentReplicationService, "_fetch_from_peer")
    def test_failover_to_second_origin(self, mock_fetch):
        """First origin fails, second succeeds."""
        mock_fetch.side_effect = [
            RuntimeError("unreachable"),
            b"content from second",
        ]
        file_meta = _make_file_meta(
            "/shared/file.txt",
            f"local@{REMOTE_ADDR},{REMOTE_ADDR_2}",
        )
        svc = _make_service(
            mount_entries=[_make_mount_fm("/shared", replication="all-voters")],
            file_entries=[file_meta],
        )

        svc._scan_and_replicate()

        assert mock_fetch.call_count == 2
        svc._object_store.write_content.assert_called_once_with(b"content from second")

    @patch.object(ContentReplicationService, "_fetch_from_peer")
    def test_all_origins_fail_logs_warning(self, mock_fetch):
        """All origins fail — no crash, just logs and continues."""
        mock_fetch.side_effect = RuntimeError("unreachable")
        file_meta = _make_file_meta(
            "/shared/file.txt",
            f"local@{REMOTE_ADDR}",
        )
        svc = _make_service(
            mount_entries=[_make_mount_fm("/shared", replication="all-voters")],
            file_entries=[file_meta],
        )

        svc._scan_and_replicate()  # should not raise

        svc._object_store.write_content.assert_not_called()
        svc._metastore.put.assert_not_called()


class TestStartStop:
    """Test the async lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        svc = _make_service(mount_entries=[], file_entries=[])
        await svc.start()
        assert svc._task is not None
        assert not svc._task.done()
        await svc.stop()
        assert svc._task is None

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        svc = _make_service(mount_entries=[], file_entries=[])
        await svc.start()
        task1 = svc._task
        await svc.start()  # second call is no-op
        assert svc._task is task1
        await svc.stop()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self):
        svc = _make_service(mount_entries=[], file_entries=[])
        await svc.stop()  # no task yet — should not raise
        await svc.start()
        await svc.stop()
        await svc.stop()  # already stopped — should not raise
