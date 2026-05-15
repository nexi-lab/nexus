"""Agent namespace fork service (Issue #1273).

Orchestrates fork/merge/discard lifecycle for agent namespace overlays.
Composes ``NamespaceManager`` (does NOT extend it) — the fork service
manages a collection of ``AgentNamespace`` overlays indexed by fork_id.

Thread safety: ``_forks`` dict is protected by ``threading.Lock``.
Individual ``AgentNamespace`` objects are single-owner (one agent).

TTL cleanup: ``cleanup_expired()`` sweeps forks older than the
configured TTL (default 30 min).  Called periodically by the scheduler
or explicitly by MCP tools.
"""

import logging
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from nexus.contracts.exceptions import (
    NamespaceForkNotFoundError,
    NamespaceMergeConflictError,
)
from nexus.contracts.namespace_fork_types import (
    ForkConflict,
    ForkMode,
    NamespaceForkInfo,
    NamespaceMergeResult,
)
from nexus.lib.merge_utils import three_way_merge_dicts
from nexus.services.namespace.agent_namespace import AgentNamespace

if TYPE_CHECKING:
    from nexus.bricks.rebac.namespace_manager import NamespaceManager

logger = logging.getLogger(__name__)


class AgentNamespaceForkService:
    """Manages the lifecycle of forked agent namespaces.

    Args:
        namespace_manager: The live ``NamespaceManager`` to snapshot from.
        ttl_seconds: Auto-cleanup TTL in seconds (default 1800 = 30 min).
    """

    def __init__(
        self,
        namespace_manager: "NamespaceManager",
        *,
        ttl_seconds: int = 1800,
    ) -> None:
        self._namespace_manager = namespace_manager
        self._ttl = timedelta(seconds=ttl_seconds)
        self._lock = threading.Lock()
        self._forks: dict[str, AgentNamespace] = {}

    def fork(
        self,
        agent_id: str,
        zone_id: str | None = None,
        *,
        parent_fork_id: str | None = None,
        mode: ForkMode = ForkMode.COPY,
    ) -> NamespaceForkInfo:
        """Create a new namespace fork.

        If *parent_fork_id* is given, snapshot from that fork's materialized
        view.  Otherwise snapshot from the live ``NamespaceManager``.

        Args:
            agent_id: Agent creating the fork.
            zone_id: Zone ID for multi-zone isolation.
            parent_fork_id: Fork to fork from (nested fork).
            mode: COPY (inherit parent) or CLEAN (empty).

        Returns:
            Immutable ``NamespaceForkInfo`` for the new fork.

        Raises:
            NamespaceForkNotFoundError: If parent_fork_id doesn't exist.
        """
        fork_id = uuid.uuid4().hex

        # Build parent snapshot
        if parent_fork_id is not None:
            with self._lock:
                parent_fork = self._forks.get(parent_fork_id)
            if parent_fork is None:
                raise NamespaceForkNotFoundError(parent_fork_id)
            parent_snapshot = parent_fork.get_all()
        else:
            # Snapshot from live NamespaceManager
            mount_entries = self._namespace_manager.get_mount_table(
                ("agent", agent_id),
                zone_id=zone_id,
            )
            parent_snapshot = {e.virtual_path: e for e in mount_entries}

        ns = AgentNamespace(
            fork_id=fork_id,
            parent_fork_id=parent_fork_id,
            agent_id=agent_id,
            zone_id=zone_id,
            mode=mode,
            parent_snapshot=parent_snapshot if mode == ForkMode.COPY else {},
        )

        with self._lock:
            self._forks[fork_id] = ns

        logger.debug(
            "[NAMESPACE:FORK] Created fork %s for agent %s (mode=%s, mounts=%d)",
            fork_id,
            agent_id,
            mode.value,
            len(parent_snapshot) if mode == ForkMode.COPY else 0,
        )
        return ns.info

    def merge(
        self,
        fork_id: str,
        *,
        strategy: str = "fail",
    ) -> NamespaceMergeResult:
        """Merge a fork back into the parent namespace.

        Uses three-way merge: base=parent_snapshot, left=fork_current,
        right=parent_current.

        Args:
            fork_id: Fork to merge.
            strategy: 'fail' (raise on conflict) or 'source-wins' (fork wins).

        Returns:
            ``NamespaceMergeResult`` with counts and any conflicts.

        Raises:
            NamespaceForkNotFoundError: If fork_id doesn't exist.
            NamespaceMergeConflictError: If conflicts and strategy='fail'.
        """
        with self._lock:
            ns = self._forks.get(fork_id)
        if ns is None:
            raise NamespaceForkNotFoundError(fork_id)

        # Three-way merge inputs
        base = ns.get_parent_snapshot()  # parent at fork time
        left = ns.get_all()  # fork's current materialized view

        # Get current parent state
        current_entries = self._namespace_manager.get_mount_table(
            ("agent", ns.agent_id),
            zone_id=ns.zone_id,
        )
        right = {e.virtual_path: e for e in current_entries}

        # Merge using string keys (MountEntry has virtual_path only)
        # Convert to string-keyed dicts for merge_utils
        base_str = {k: k for k in base}
        left_str = {k: k for k in left}
        right_str = {k: k for k in right}

        merged_str, conflict_keys = three_way_merge_dicts(
            base_str,
            left_str,
            right_str,
            strategy=strategy,
        )

        if conflict_keys and strategy == "fail":
            raise NamespaceMergeConflictError(fork_id, conflict_keys)

        # Compute counts
        base_keys = set(base_str)
        merged_keys = set(merged_str)
        added = len(merged_keys - base_keys)
        removed = len(base_keys - merged_keys)
        modified = sum(1 for k in (base_keys & merged_keys) if merged_str[k] != base_str.get(k))

        # Build conflict info for result (empty for source-wins)
        conflicts = tuple(
            ForkConflict(
                path=k,
                parent_value=_mount_path(base, k),
                fork_value=_mount_path(left, k),
                current_parent_value=_mount_path(right, k),
            )
            for k in conflict_keys
        )

        # Apply merged mount table back to the live namespace.
        # For each merged key, prefer the fork's entry (left), fall back to parent (right).
        merged_entries = [left.get(k) or right.get(k) for k in sorted(merged_str)]
        merged_mount_entries = [e for e in merged_entries if e is not None]
        self._namespace_manager.update_mount_table(
            ("agent", ns.agent_id),
            merged_mount_entries,
            zone_id=ns.zone_id,
        )

        # Clean up fork
        with self._lock:
            self._forks.pop(fork_id, None)

        logger.debug(
            "[NAMESPACE:FORK] Merged fork %s (+%d -%d ~%d, strategy=%s)",
            fork_id,
            added,
            removed,
            modified,
            strategy,
        )

        return NamespaceMergeResult(
            merged=True,
            fork_id=fork_id,
            entries_added=added,
            entries_removed=removed,
            entries_modified=modified,
            conflicts=conflicts,
            strategy=strategy,
        )

    def discard(self, fork_id: str) -> None:
        """Discard a fork without merging. O(1).

        Raises:
            NamespaceForkNotFoundError: If fork_id doesn't exist.
        """
        with self._lock:
            if fork_id not in self._forks:
                raise NamespaceForkNotFoundError(fork_id)
            del self._forks[fork_id]

        logger.debug("[NAMESPACE:FORK] Discarded fork %s", fork_id)

    def get_fork(self, fork_id: str) -> AgentNamespace:
        """Get the live ``AgentNamespace`` object for direct read/write.

        Raises:
            NamespaceForkNotFoundError: If fork_id doesn't exist.
        """
        with self._lock:
            ns = self._forks.get(fork_id)
        if ns is None:
            raise NamespaceForkNotFoundError(fork_id)
        return ns

    def get_fork_info(self, fork_id: str) -> NamespaceForkInfo:
        """Get immutable metadata for a fork.

        Raises:
            NamespaceForkNotFoundError: If fork_id doesn't exist.
        """
        return self.get_fork(fork_id).info

    def list_forks(self, agent_id: str | None = None) -> list[NamespaceForkInfo]:
        """List all active forks, optionally filtered by agent_id."""
        with self._lock:
            forks = list(self._forks.values())
        if agent_id is not None:
            forks = [f for f in forks if f.agent_id == agent_id]
        return [f.info for f in forks]

    def cleanup_expired(self) -> int:
        """Remove forks older than the TTL. Returns count of removed forks."""
        now = datetime.now(UTC)
        expired: list[str] = []

        with self._lock:
            for fork_id, ns in self._forks.items():
                if (now - ns.created_at) > self._ttl:
                    expired.append(fork_id)
            for fork_id in expired:
                del self._forks[fork_id]

        if expired:
            logger.info("[NAMESPACE:FORK] Cleaned up %d expired forks", len(expired))
        return len(expired)


def _mount_path(
    entries: Mapping[str, object],
    key: str,
) -> str | None:
    """Extract virtual_path string from a mount entry dict, or None."""
    entry = entries.get(key)
    if entry is None:
        return None
    return getattr(entry, "virtual_path", None)
