"""Snapshots RPC Service — create, list, restore transactional snapshots.

Issue #1520.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class SnapshotsRPCService:
    """RPC surface for transactional snapshot operations."""

    def __init__(self, snapshot_service: Any) -> None:
        self._snapshot_service = snapshot_service

    @rpc_expose(description="Create a transactional snapshot")
    async def snapshot_create(
        self,
        description: str | None = None,
        ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        info = await self._snapshot_service.begin(
            description=description,
            ttl_seconds=ttl_seconds,
        )
        return self._txn_to_dict(info)

    @rpc_expose(description="List snapshot transactions")
    async def snapshot_list(self) -> dict[str, Any]:
        txns = await self._snapshot_service.list_transactions()
        return {
            "transactions": [self._txn_to_dict(t) for t in txns],
            "count": len(txns),
        }

    @rpc_expose(description="Rollback a snapshot transaction")
    async def snapshot_restore(self, txn_id: str) -> dict[str, Any]:
        info = await self._snapshot_service.rollback(txn_id)
        return self._txn_to_dict(info)

    @rpc_expose(description="Get transaction details")
    async def snapshot_get(self, transaction_id: str) -> dict[str, Any]:
        info = await self._snapshot_service.get_transaction(transaction_id)
        if info is None:
            return {"found": False}
        return self._txn_to_dict(info)

    @rpc_expose(description="Commit a transaction")
    async def snapshot_commit(self, transaction_id: str) -> dict[str, Any]:
        info = await self._snapshot_service.commit(transaction_id)
        return self._txn_to_dict(info)

    @rpc_expose(description="List entries in a transaction")
    async def snapshot_list_entries(self, transaction_id: str) -> dict[str, Any]:
        entries = await self._snapshot_service.list_entries(transaction_id)
        return {"entries": [self._entry_to_dict(e) for e in entries], "count": len(entries)}

    @staticmethod
    def _entry_to_dict(entry: Any) -> dict[str, Any]:
        if isinstance(entry, dict):
            return entry
        return {
            k: str(v) if v is not None else None
            for k, v in (entry.__dict__ if hasattr(entry, "__dict__") else {"value": entry}).items()
        }

    @staticmethod
    def _txn_to_dict(info: Any) -> dict[str, Any]:
        if isinstance(info, dict):
            return info
        return {
            k: str(v) if v is not None else None
            for k, v in (info.__dict__ if hasattr(info, "__dict__") else {"value": info}).items()
        }
