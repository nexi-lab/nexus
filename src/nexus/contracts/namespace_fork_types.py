"""Namespace fork types for agent speculative execution (Issue #1273).

Pure value objects for namespace forking — Plan 9 ``rfork(RFNAMEG)`` inspired.
Zero runtime dependencies — only stdlib imports.

ForkMode selects the initial state of the forked mount table:
    COPY: Eager ``dict()`` copy of parent mounts (~50us/1K entries).
    CLEAN: Empty overlay — agent builds namespace from scratch.

Design:
    - Frozen dataclasses with ``__slots__`` for immutability and memory efficiency
    - ``tuple`` for immutable collections (not ``list``)
    - Zero kernel imports — contracts module only
"""

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime


class ForkMode(enum.Enum):
    """How the forked namespace is initialized from the parent.

    COPY: Eager snapshot of all parent mounts.  The fork starts with
          full visibility and can selectively add/remove paths.
    CLEAN: Empty overlay.  The fork starts with zero visibility and
           must explicitly add paths.  Useful for sandboxed execution.
    """

    COPY = "copy"
    CLEAN = "clean"


@dataclass(frozen=True, slots=True)
class NamespaceForkInfo:
    """Immutable snapshot of a namespace fork's metadata.

    Returned by ``fork()`` and ``get_fork_info()`` — never exposes
    internal overlay/snapshot state.

    Attributes:
        fork_id: Unique identifier for this fork (UUID4 hex).
        parent_fork_id: Fork ID of the parent (None if forked from live namespace).
        agent_id: Agent that owns this fork.
        zone_id: Zone/organization ID for multi-zone isolation.
        mode: How the fork was initialized (COPY or CLEAN).
        created_at: UTC timestamp when the fork was created.
        mount_count: Number of visible paths in the materialized view at info time.
    """

    fork_id: str
    parent_fork_id: str | None
    agent_id: str
    zone_id: str | None
    mode: ForkMode
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    mount_count: int = 0


@dataclass(frozen=True, slots=True)
class ForkConflict:
    """A single path conflict detected during three-way merge.

    Attributes:
        path: The conflicting mount path.
        parent_value: Value at fork time (base).
        fork_value: Value in the fork overlay (left).
        current_parent_value: Current value in the parent (right).
    """

    path: str
    parent_value: str | None
    fork_value: str | None
    current_parent_value: str | None


@dataclass(frozen=True, slots=True)
class NamespaceMergeResult:
    """Outcome of merging a namespace fork back into the parent.

    Attributes:
        merged: True if merge completed successfully.
        fork_id: The fork that was merged.
        entries_added: Number of new paths added to parent.
        entries_removed: Number of paths removed from parent.
        entries_modified: Number of paths modified in parent.
        conflicts: Tuple of conflicts (empty on success or source-wins).
        strategy: Merge strategy used ('fail' or 'source-wins').
    """

    merged: bool
    fork_id: str
    entries_added: int = 0
    entries_removed: int = 0
    entries_modified: int = 0
    conflicts: tuple[ForkConflict, ...] = ()
    strategy: str = "fail"
