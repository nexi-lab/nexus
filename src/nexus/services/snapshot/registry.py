"""In-memory transaction registry for fast-path lookups (Issue #1752).

Thread-safe dict-based registry providing O(1) path → transaction_id lookups.
Key design: **zero cost for non-transactional writes** via has_active_transactions()
early exit.

The outermost check ``has_active_transactions()`` is a single ``bool(dict)`` call
(< 1 ns). When no transactions are active (99.9% of operations), the write path
skips all snapshot logic entirely.
"""

import threading

class TransactionRegistry:
    """Thread-safe in-memory registry mapping paths to active transactions.

    Provides O(1) fast-path for the write path to determine if a path
    is currently tracked by an active transaction.
    """

    __slots__ = ("_by_txn_id", "_lock", "_path_to_txn")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_txn_id: dict[str, set[str]] = {}
        self._path_to_txn: dict[str, str] = {}

    def register(self, transaction_id: str) -> None:
        """Register a new transaction (no paths yet)."""
        with self._lock:
            if transaction_id not in self._by_txn_id:
                self._by_txn_id[transaction_id] = set()

    def track_path(self, transaction_id: str, path: str) -> bool:
        """Associate a path with a transaction.

        Returns True if the path was successfully tracked.
        Returns False if the path is already tracked by a *different* transaction
        (conflict — optimistic concurrency violation).
        """
        with self._lock:
            existing_txn = self._path_to_txn.get(path)
            if existing_txn is not None and existing_txn != transaction_id:
                return False

            self._path_to_txn[path] = transaction_id
            paths = self._by_txn_id.get(transaction_id)
            if paths is not None:
                paths.add(path)
            return True

    def get_transaction_for_path(self, path: str) -> str | None:
        """Return the transaction_id tracking this path, or None.

        Note: dict.get() is atomic under CPython GIL. This method
        is intentionally lock-free for hot-path performance.
        """
        return self._path_to_txn.get(path)

    def get_paths(self, transaction_id: str) -> frozenset[str]:
        """Return all paths tracked by a transaction."""
        with self._lock:
            paths = self._by_txn_id.get(transaction_id)
            return frozenset(paths) if paths is not None else frozenset()

    def unregister(self, transaction_id: str) -> frozenset[str]:
        """Remove a transaction and all its tracked paths.

        Returns the set of paths that were being tracked.
        """
        with self._lock:
            paths = self._by_txn_id.pop(transaction_id, None)
            if paths is None:
                return frozenset()
            for p in paths:
                if self._path_to_txn.get(p) == transaction_id:
                    del self._path_to_txn[p]
            return frozenset(paths)

    def has_active_transactions(self) -> bool:
        """Fast-path check: are there any active transactions?

        This is the outermost guard in the write path. When False (99.9% case),
        all snapshot logic is skipped. Cost: single bool(dict) < 1 ns.

        Note: bool(dict) is atomic under CPython GIL. Intentionally lock-free.
        """
        return bool(self._by_txn_id)

    @property
    def active_count(self) -> int:
        """Number of active transactions (for metrics)."""
        return len(self._by_txn_id)

    @property
    def tracked_path_count(self) -> int:
        """Number of tracked paths across all transactions (for metrics)."""
        return len(self._path_to_txn)
