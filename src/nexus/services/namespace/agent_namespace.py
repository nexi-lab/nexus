"""Copy-on-write agent namespace overlay (Issue #1273).

Each ``AgentNamespace`` represents a forked mount table: a frozen
snapshot of the parent plus a mutable overlay and tombstone set.
Reads fall through to the parent snapshot (COPY mode) or return
nothing (CLEAN mode).  Writes only touch the overlay — the parent
snapshot is never mutated.

Thread safety: a single ``AgentNamespace`` is owned by one agent
and should not be shared across threads.  The ``AgentNamespaceForkService``
holds the collection of forks behind a ``threading.Lock``.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from nexus.bricks.rebac.namespace_manager import MountEntry

from nexus.contracts.namespace_fork_types import ForkMode, NamespaceForkInfo

_MISSING = object()


class AgentNamespace:
    """Copy-on-write overlay for a forked mount table.

    Attributes (via ``__slots__``):
        fork_id: Unique fork identifier.
        parent_fork_id: Parent fork ID (None if forked from live namespace).
        agent_id: Owning agent.
        zone_id: Zone/org ID.
        mode: COPY or CLEAN.
        created_at: UTC creation timestamp.
    """

    __slots__ = (
        "fork_id",
        "parent_fork_id",
        "agent_id",
        "zone_id",
        "mode",
        "created_at",
        "_parent_snapshot",
        "_overlay",
        "_deleted_keys",
    )

    def __init__(
        self,
        *,
        fork_id: str,
        parent_fork_id: str | None,
        agent_id: str,
        zone_id: str | None,
        mode: ForkMode,
        parent_snapshot: "dict[str, MountEntry]",
    ) -> None:
        self.fork_id = fork_id
        self.parent_fork_id = parent_fork_id
        self.agent_id = agent_id
        self.zone_id = zone_id
        self.mode = mode
        self.created_at = datetime.now(UTC)

        # Eager copy — ~50us/1K entries
        self._parent_snapshot: dict[str, MountEntry] = dict(parent_snapshot)
        self._overlay: dict[str, MountEntry] = {}
        self._deleted_keys: set[str] = set()

    def get(self, path: str) -> "MountEntry | None":
        """Read a single path.

        Lookup order: deleted_keys → overlay → parent_snapshot.
        CLEAN mode skips parent_snapshot (fork starts empty).
        """
        deleted_keys = self._deleted_keys
        if path in deleted_keys:
            return None
        overlay_entry: MountEntry | object = self._overlay.get(path, _MISSING)
        if overlay_entry is not _MISSING:
            return cast("MountEntry", overlay_entry)
        if self.mode == ForkMode.CLEAN:
            return None
        return self._parent_snapshot.get(path)

    def put(self, path: str, entry: "MountEntry") -> None:
        """Write to the overlay. Never mutates the parent snapshot."""
        self._overlay[path] = entry
        self._deleted_keys.discard(path)

    def delete(self, path: str) -> None:
        """Tombstone a path — marks it invisible in this fork."""
        self._deleted_keys.add(path)
        self._overlay.pop(path, None)

    def get_all(self) -> "dict[str, MountEntry]":
        """Materialized view: parent + overlay - deletions.

        Returns a new dict (never a reference to internal state).
        """
        if self.mode == ForkMode.CLEAN:
            # CLEAN: only overlay minus deletions
            return {k: v for k, v in self._overlay.items() if k not in self._deleted_keys}
        # COPY: merge parent + overlay - deletions
        result = {k: v for k, v in self._parent_snapshot.items() if k not in self._deleted_keys}
        result.update(self._overlay)
        # Overlay might have keys also in deleted_keys if put() after delete()
        # but put() already removes from deleted_keys, so this is clean
        return result

    @property
    def info(self) -> NamespaceForkInfo:
        """Immutable metadata snapshot."""
        return NamespaceForkInfo(
            fork_id=self.fork_id,
            parent_fork_id=self.parent_fork_id,
            agent_id=self.agent_id,
            zone_id=self.zone_id,
            mode=self.mode,
            created_at=self.created_at,
            mount_count=len(self.get_all()),
        )

    def get_overlay(self) -> "dict[str, MountEntry]":
        """Return a copy of the overlay dict."""
        return dict(self._overlay)

    def get_deleted_keys(self) -> set[str]:
        """Return a copy of the tombstone set."""
        return set(self._deleted_keys)

    def get_parent_snapshot(self) -> "dict[str, MountEntry]":
        """Return a copy of the parent snapshot."""
        return dict(self._parent_snapshot)
