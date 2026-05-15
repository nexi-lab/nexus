"""Unit tests for SnapshotsRPCService — snapshot_get, snapshot_commit, snapshot_list_entries.

Issue #1528.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from nexus.server.rpc.services.snapshots_rpc import SnapshotsRPCService


@dataclass
class _FakeTxn:
    transaction_id: str = "txn-001"
    status: str = "active"
    description: str = "test"


@dataclass
class _FakeEntry:
    path: str = "/corp/file.txt"
    operation: str = "write"
    content_id: str = "abc123"


@pytest.fixture()
def snapshot_service():
    svc = AsyncMock()
    svc.get_transaction = AsyncMock(return_value=_FakeTxn())
    svc.commit = AsyncMock(return_value=_FakeTxn(status="committed"))
    svc.list_entries = AsyncMock(return_value=[_FakeEntry(), _FakeEntry(path="/corp/other.txt")])
    return svc


@pytest.fixture()
def svc(snapshot_service):
    return SnapshotsRPCService(snapshot_service)


class TestSnapshotGet:
    @pytest.mark.asyncio
    async def test_get_found(self, svc, snapshot_service):
        result = await svc.snapshot_get(transaction_id="txn-001")
        assert result["transaction_id"] == "txn-001"
        assert result["status"] == "active"
        snapshot_service.get_transaction.assert_awaited_once_with("txn-001")

    @pytest.mark.asyncio
    async def test_get_not_found(self, svc, snapshot_service):
        snapshot_service.get_transaction.return_value = None
        result = await svc.snapshot_get(transaction_id="missing")
        assert result["found"] is False


class TestSnapshotCommit:
    @pytest.mark.asyncio
    async def test_commit(self, svc, snapshot_service):
        result = await svc.snapshot_commit(transaction_id="txn-001")
        assert result["status"] == "committed"
        snapshot_service.commit.assert_awaited_once_with("txn-001")


class TestSnapshotListEntries:
    @pytest.mark.asyncio
    async def test_list_entries(self, svc, snapshot_service):
        result = await svc.snapshot_list_entries(transaction_id="txn-001")
        assert result["count"] == 2
        assert len(result["entries"]) == 2
        assert result["entries"][0]["path"] == "/corp/file.txt"
        snapshot_service.list_entries.assert_awaited_once_with("txn-001")

    @pytest.mark.asyncio
    async def test_list_entries_empty(self, svc, snapshot_service):
        snapshot_service.list_entries.return_value = []
        result = await svc.snapshot_list_entries(transaction_id="txn-002")
        assert result["count"] == 0
        assert result["entries"] == []

    @pytest.mark.asyncio
    async def test_list_entries_dict_passthrough(self, svc, snapshot_service):
        snapshot_service.list_entries.return_value = [{"path": "/a", "op": "write"}]
        result = await svc.snapshot_list_entries(transaction_id="txn-003")
        assert result["entries"][0] == {"path": "/a", "op": "write"}
