"""Namespace pagination stays bounded and preserves lexical cursor order."""

from typing import Any

import pytest

from nexus.contracts.metadata import DT_DIR, DT_REG
from nexus.grpc.vfs import vfs_pb2
from nexus.remote.vfs_client import RemoteFilesystemClient


class DirectoryHost:
    def __init__(self):
        self.directories = {
            "/": [("/z", DT_DIR), ("/a-b", DT_REG), ("/a", DT_DIR)],
            "/a": [("/a/1", DT_REG)],
            "/z": [("/z/last", DT_REG)],
        }
        self.walks = []
        self.stat_batches = []

    def readdir(self, path):
        self.walks.append(path)
        return [
            vfs_pb2.ReaddirEntry(name=name, entry_type=kind)
            for name, kind in self.directories[path]
        ]

    def batch_stat(self, paths):
        self.stat_batches.append(paths)
        return [
            vfs_pb2.BatchStatItem(
                found=True, path=path, entry_type=DT_DIR if path in self.directories else DT_REG
            )
            for path in paths
        ]


def test_directory_page_reads_only_the_needed_subtree_and_batches_page_metadata():
    host: Any = DirectoryHost()
    client = RemoteFilesystemClient(host)
    first = client.sys_readdir("/", limit=2, details=True)
    assert [item["path"] for item in first.items] == ["/a", "/a-b"]
    assert first.has_more and first.next_cursor == "/a-b" and first.total_count is None
    assert host.walks == ["/", "/a"]
    assert host.stat_batches == [["/a", "/a-b"]]
    second = client.sys_readdir("/", limit=2, cursor=first.next_cursor)
    assert second.items == ["/a/1", "/z"]
    assert second.has_more and second.next_cursor == "/z"
    last = client.sys_readdir("/", limit=2, cursor=second.next_cursor)
    assert last.items == ["/z/last"] and not last.has_more


def test_unbounded_listing_visits_each_directory_once_and_keeps_siblings_in_order():
    host: Any = DirectoryHost()
    assert RemoteFilesystemClient(host).sys_readdir("/") == ["/a", "/a-b", "/a/1", "/z", "/z/last"]
    assert sorted(host.walks) == ["/", "/a", "/z"]
    assert host.stat_batches == []


def test_directory_denial_propagates_without_returning_a_partial_page():
    host: Any = DirectoryHost()

    def denied(path):
        raise PermissionError(path)

    host.readdir = denied
    with pytest.raises(PermissionError, match="/a"):
        RemoteFilesystemClient(host).sys_readdir("/a", limit=2)
