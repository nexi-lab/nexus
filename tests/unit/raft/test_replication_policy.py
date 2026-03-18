"""Unit tests for ReplicationPolicyResolver."""

import json
from unittest.mock import MagicMock

from nexus.contracts.metadata import FileMetadata
from nexus.raft.replication_policy import ReplicationPolicyResolver


def _make_mount_fm(mount_point: str, replication: str | None = None) -> FileMetadata:
    """Create a mount config FileMetadata entry."""
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


class TestReplicationPolicyResolver:
    def test_empty_metastore(self):
        metastore = MagicMock()
        metastore.list.return_value = []

        resolver = ReplicationPolicyResolver(metastore)
        resolver.refresh()

        assert resolver.get_policy("/any/path") is None
        assert resolver.get_replicated_prefixes() == []

    def test_no_replication_policy(self):
        metastore = MagicMock()
        metastore.list.return_value = [_make_mount_fm("/data")]

        resolver = ReplicationPolicyResolver(metastore)
        resolver.refresh()

        assert resolver.get_policy("/data/file.txt") is None
        assert resolver.get_replicated_prefixes() == []

    def test_all_voters_policy(self):
        metastore = MagicMock()
        metastore.list.return_value = [
            _make_mount_fm("/shared", replication="all-voters"),
        ]

        resolver = ReplicationPolicyResolver(metastore)
        resolver.refresh()

        assert resolver.get_policy("/shared/memory.md") == "all-voters"
        assert resolver.get_policy("/other/file.txt") is None
        assert resolver.get_replicated_prefixes() == ["/shared"]

    def test_longest_prefix_match(self):
        metastore = MagicMock()
        metastore.list.return_value = [
            _make_mount_fm("/data", replication=None),
            _make_mount_fm("/data/shared", replication="all-voters"),
        ]

        resolver = ReplicationPolicyResolver(metastore)
        resolver.refresh()

        # /data/shared/file → matches /data/shared (longer) → all-voters
        assert resolver.get_policy("/data/shared/file.txt") == "all-voters"
        # /data/other/file → matches /data (only match) → None
        assert resolver.get_policy("/data/other/file.txt") is None

    def test_refresh_replaces_cache(self):
        metastore = MagicMock()
        metastore.list.return_value = [
            _make_mount_fm("/shared", replication="all-voters"),
        ]

        resolver = ReplicationPolicyResolver(metastore)
        resolver.refresh()
        assert resolver.get_policy("/shared/file.txt") == "all-voters"

        # Refresh with no mounts
        metastore.list.return_value = []
        resolver.refresh()
        assert resolver.get_policy("/shared/file.txt") is None

    def test_skips_non_mount_entries(self):
        metastore = MagicMock()
        non_mount = FileMetadata(
            path="mnt:/something",
            backend_name="not_mount_config",
            physical_path="{}",
            size=0,
        )
        metastore.list.return_value = [non_mount]

        resolver = ReplicationPolicyResolver(metastore)
        resolver.refresh()

        assert resolver.get_replicated_prefixes() == []

    def test_skips_malformed_json(self):
        metastore = MagicMock()
        bad_fm = FileMetadata(
            path="mnt:/broken",
            backend_name="_mount_config",
            physical_path="not-json",
            size=0,
        )
        metastore.list.return_value = [bad_fm]

        resolver = ReplicationPolicyResolver(metastore)
        resolver.refresh()

        assert resolver.get_replicated_prefixes() == []
